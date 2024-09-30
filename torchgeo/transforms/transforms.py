# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

"""TorchGeo transforms."""

from typing import Any

import kornia.augmentation as K
import torch
from einops import rearrange
from kornia.contrib import extract_tensor_patches
from kornia.geometry import crop_by_indices
from kornia.geometry.boxes import Boxes
from torch import Tensor
from torch.nn.modules import Module


# TODO: contribute these to Kornia and delete this file
class AugmentationSequential(Module):
    """Wrapper around kornia AugmentationSequential to handle input dicts.

    .. deprecated:: 0.4
       Use :class:`kornia.augmentation.container.AugmentationSequential` instead.
    """

    def __init__(
        self,
        *args: K.base._AugmentationBase | K.ImageSequential,
        data_keys: list[str],
        **kwargs: Any,
    ) -> None:
        """Initialize a new augmentation sequential instance.

        Args:
            *args: Sequence of kornia augmentations
            data_keys: List of inputs to augment (e.g., ["image", "mask", "boxes"])
            **kwargs: Keyword arguments passed to ``K.AugmentationSequential``

        .. versionadded:: 0.5
           The ``**kwargs`` parameter.
        """
        super().__init__()
        self.data_keys = data_keys

        keys: list[str] = []
        for key in data_keys:
            if key.startswith('image'):
                keys.append('input')
            elif key == 'boxes':
                keys.append('bbox')
            elif key == 'masks':
                keys.append('mask')
            else:
                keys.append(key)

        self.augs = K.AugmentationSequential(*args, data_keys=keys, **kwargs)

    def forward(self, batch: dict[str, Any]) -> dict[str, Any]:
        """Perform augmentations and update data dict.

        Args:
            batch: the input

        Returns:
            the augmented input
        """
        # Kornia augmentations require all inputs to be float
        dtype = {}
        for key in self.data_keys:
            dtype[key] = batch[key].dtype
            batch[key] = batch[key].float()

        # Convert shape of boxes from [N, 4] to [N, 4, 2]
        if 'boxes' in batch and (
            isinstance(batch['boxes'], list) or batch['boxes'].ndim == 2
        ):
            batch['boxes'] = Boxes.from_tensor(batch['boxes']).data

        # Kornia requires masks to have a channel dimension
        if 'mask' in batch and batch['mask'].ndim == 3:
            batch['mask'] = rearrange(batch['mask'], 'b h w -> b () h w')

        if 'masks' in batch and batch['masks'].ndim == 3:
            batch['masks'] = rearrange(batch['masks'], 'c h w -> () c h w')

        inputs = [batch[k] for k in self.data_keys]
        outputs_list: Tensor | list[Tensor] = self.augs(*inputs)
        outputs_list = (
            outputs_list if isinstance(outputs_list, list) else [outputs_list]
        )
        outputs: dict[str, Tensor] = {
            k: v for k, v in zip(self.data_keys, outputs_list)
        }
        batch.update(outputs)

        # Convert all inputs back to their previous dtype
        for key in self.data_keys:
            batch[key] = batch[key].to(dtype[key])

        # Convert boxes to default [N, 4]
        if 'boxes' in batch:
            batch['boxes'] = Boxes(batch['boxes']).to_tensor(mode='xyxy')

        # Torchmetrics does not support masks with a channel dimension
        if 'mask' in batch and batch['mask'].shape[1] == 1:
            batch['mask'] = rearrange(batch['mask'], 'b () h w -> b h w')
        if 'masks' in batch and batch['masks'].ndim == 4:
            batch['masks'] = rearrange(batch['masks'], '() c h w -> c h w')

        return batch


class _RandomNCrop(K.GeometricAugmentationBase2D):
    """Take N random crops of a tensor."""

    def __init__(self, size: tuple[int, int], num: int) -> None:
        """Initialize a new _RandomNCrop instance.

        Args:
            size: desired output size (out_h, out_w) of the crop
            num: number of crops to take
        """
        super().__init__(p=1)
        self._param_generator: _NCropGenerator = _NCropGenerator(size, num)
        self.flags = {'size': size, 'num': num}

    def compute_transformation(
        self, input: Tensor, params: dict[str, Tensor], flags: dict[str, Any]
    ) -> Tensor:
        """Compute the transformation.

        Args:
            input: the input tensor
            params: generated parameters
            flags: static parameters

        Returns:
            the transformation
        """
        out: Tensor = self.identity_matrix(input)
        return out

    def apply_transform(
        self,
        input: Tensor,
        params: dict[str, Tensor],
        flags: dict[str, Any],
        transform: Tensor | None = None,
    ) -> Tensor:
        """Apply the transform.

        Args:
            input: the input tensor
            params: generated parameters
            flags: static parameters
            transform: the geometric transformation tensor

        Returns:
            the augmented input
        """
        out = []
        for i in range(flags['num']):
            out.append(crop_by_indices(input, params['src'][i], flags['size']))
        return torch.cat(out)


class _NCropGenerator(K.random_generator.CropGenerator):
    """Generate N random crops."""

    def __init__(self, size: tuple[int, int] | Tensor, num: int) -> None:
        """Initialize a new _NCropGenerator instance.

        Args:
            size: desired output size (out_h, out_w) of the crop
            num: number of crops to generate
        """
        super().__init__(size)
        self.num = num

    def forward(
        self, batch_shape: tuple[int, ...], same_on_batch: bool = False
    ) -> dict[str, Tensor]:
        """Generate the crops.

        Args:
            batch_shape: input size (b, c?, in_h, in_w)
            same_on_batch: apply the same transformation across the batch

        Returns:
            the randomly generated parameters
        """
        out = []
        for _ in range(self.num):
            out.append(super().forward(batch_shape, same_on_batch))
        return {
            'src': torch.stack([x['src'] for x in out]),
            'dst': torch.stack([x['dst'] for x in out]),
            'input_size': out[0]['input_size'],
            'output_size': out[0]['output_size'],
        }


class _ExtractPatches(K.GeometricAugmentationBase2D):
    """Extract patches from an image or mask."""

    def __init__(
        self,
        window_size: int | tuple[int, int],
        stride: int | tuple[int, int] | None = None,
        padding: int | tuple[int, int] | None = 0,
        keepdim: bool = True,
    ) -> None:
        """Initialize a new _ExtractPatches instance.

        Args:
            window_size: desired output size (out_h, out_w) of the crop
            stride: stride of window to extract patches. Defaults to non-overlapping
                patches (stride=window_size)
            padding: zero padding added to the height and width dimensions
            keepdim: Combine the patch dimension into the batch dimension
        """
        super().__init__(p=1)
        self.flags = {
            'window_size': window_size,
            'stride': stride if stride is not None else window_size,
            'padding': padding,
            'keepdim': keepdim,
        }

    def compute_transformation(
        self, input: Tensor, params: dict[str, Tensor], flags: dict[str, Any]
    ) -> Tensor:
        """Compute the transformation.

        Args:
            input: the input tensor
            params: generated parameters
            flags: static parameters

        Returns:
            the transformation
        """
        out: Tensor = self.identity_matrix(input)
        return out

    def apply_transform(
        self,
        input: Tensor,
        params: dict[str, Tensor],
        flags: dict[str, Any],
        transform: Tensor | None = None,
    ) -> Tensor:
        """Apply the transform.

        Args:
            input: the input tensor
            params: generated parameters
            flags: static parameters
            transform: the geometric transformation tensor

        Returns:
            the augmented input
        """
        out = extract_tensor_patches(
            input,
            window_size=flags['window_size'],
            stride=flags['stride'],
            padding=flags['padding'],
        )

        if flags['keepdim']:
            out = rearrange(out, 'b t c h w -> (b t) c h w')

        return out


class _Clamp(K.IntensityAugmentationBase2D):
    """Clamp images to a specific range."""

    def __init__(
        self,
        p: float = 0.5,
        p_batch: float = 1,
        min: float = 0,
        max: float = 1,
        same_on_batch: bool = False,
        keepdim: bool = False,
    ) -> None:
        """Initialize a new _Clamp instance.

        Args:
            p: Probability for applying an augmentation. This param controls the
                augmentation probabilities element-wise for a batch.
            p_batch: Probability for applying an augmentation to a batch. This param
                controls the augmentation probabilities batch-wise.
            min: Minimum value to clamp to.
            max: Maximum value to clamp to.
            same_on_batch: Apply the same transformation across the batch.
            keepdim: Whether to keep the output shape the same as input ``True``
                or broadcast it to the batch form ``False``.
        """
        super().__init__(
            p=p, p_batch=p_batch, same_on_batch=same_on_batch, keepdim=keepdim
        )
        self.flags = {'min': min, 'max': max}

    def apply_transform(
        self,
        input: Tensor,
        params: dict[str, Tensor],
        flags: dict[str, Any],
        transform: Tensor | None = None,
    ) -> Tensor:
        """Apply the transform.

        Args:
            input: the input tensor
            params: generated parameters
            flags: static parameters
            transform: the geometric transformation tensor

        Returns:
            the augmented input
        """
        return torch.clamp(input, self.flags['min'], self.flags['max'])