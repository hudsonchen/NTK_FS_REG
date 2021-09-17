import types
from typing import Mapping, Optional, Sequence, Union, Any

from haiku._src import basic
from haiku._src import batch_norm
from haiku._src import conv
from haiku._src import module
from haiku._src import pool
import jax
import jax.numpy as jnp

# If forking replace this block with `import haiku as hk`.
hk = types.ModuleType("haiku")
hk.Module = module.Module
hk.BatchNorm = batch_norm.BatchNorm
hk.Conv2D = conv.Conv2D
hk.Linear = basic.Linear
hk.max_pool = pool.max_pool
hk.avg_pool = pool.avg_pool
del basic, batch_norm, conv, module, pool
FloatStrOrBool = Union[str, float, bool]


class BlockV1(hk.Module):
    """ResNet V1 block with optional bottleneck."""

    def __init__(
            self,
            channels: int,
            stride: Union[int, Sequence[int]],
            use_projection: bool,
            bn_config: Mapping[str, FloatStrOrBool],
            bottleneck: bool,
            use_bn: bool,
            name: Optional[str] = None,
    ):
        super().__init__(name=name)
        self.use_projection = use_projection
        self.use_bn = use_bn

        bn_config = dict(bn_config)
        bn_config.setdefault("create_scale", True)
        bn_config.setdefault("create_offset", True)
        bn_config.setdefault("decay_rate", 0.999)

        if self.use_projection:
            self.proj_conv = hk.Conv2D(
                output_channels=channels,
                kernel_shape=1,
                stride=stride,
                with_bias=False,
                padding="SAME",
                name="shortcut_conv")

            if use_bn:
                self.proj_batchnorm = hk.BatchNorm(name="shortcut_batchnorm", **bn_config)

        channel_div = 4 if bottleneck else 1
        conv_0 = hk.Conv2D(
            output_channels=channels // channel_div,
            kernel_shape=1 if bottleneck else 3,
            stride=1 if bottleneck else stride,
            with_bias=False,
            padding="SAME",
            name="conv_0")

        if use_bn:
            bn_0 = hk.BatchNorm(name="batchnorm_0", **bn_config)

        conv_1 = hk.Conv2D(
            output_channels=channels // channel_div,
            kernel_shape=3,
            stride=stride if bottleneck else 1,
            with_bias=False,
            padding="SAME",
            name="conv_1")

        if use_bn:
            bn_1 = hk.BatchNorm(name="batchnorm_1", **bn_config)
            layers = ((conv_0, bn_0), (conv_1, bn_1))
        else:
            layers = ((conv_0, None), (conv_1, None))

        self.layers = layers

    def __call__(self, inputs, is_training, test_local_stats):
        out = shortcut = inputs

        if self.use_projection:
            shortcut = self.proj_conv(shortcut)
            if self.use_bn:
                shortcut = self.proj_batchnorm(shortcut, is_training, test_local_stats)

        for i, (conv_i, bn_i) in enumerate(self.layers):
            out = conv_i(out)
            if self.use_bn:
                out = bn_i(out, is_training, test_local_stats)
            if i < len(self.layers) - 1:  # Don't apply relu on last layer
                out = jax.nn.relu(out)

        return jax.nn.relu(out + shortcut)


class BlockGroup(hk.Module):
    """Higher level block for ResNet implementation."""

    def __init__(
            self,
            channels: int,
            num_blocks: int,
            stride: Union[int, Sequence[int]],
            bn_config: Mapping[str, FloatStrOrBool],
            resnet_v2: bool,
            bottleneck: bool,
            use_projection: bool,
            use_bn: bool,
            name: Optional[str] = None,
    ):
        super().__init__(name=name)
        self.use_bn = use_bn
        block_cls = BlockV1

        self.blocks = []
        for i in range(num_blocks):
            self.blocks.append(
                block_cls(channels=channels,
                          stride=(1 if i else stride),
                          use_projection=use_projection,
                          bottleneck=bottleneck,
                          bn_config=bn_config,
                          use_bn=use_bn,
                          name="block_%d" % (i)))

    def __call__(self, inputs, is_training, test_local_stats):
        out = inputs
        for block in self.blocks:
            out = block(out, is_training, test_local_stats)
        return out


def check_length(length, value, name):
    if len(value) != length:
        raise ValueError(f"`{name}` must be of length 4 not {len(value)}")


class ResNet(hk.Module):
    """ResNet model."""

    BlockGroup = BlockGroup  # pylint: disable=invalid-name
    BlockV1 = BlockV1  # pylint: disable=invalid-name

    def __init__(
            self,
            blocks_per_group: Sequence[int],
            num_classes: int,
            use_bn: bool,
            resnet_v1: bool = False,
            bn_config: Optional[Mapping[str, FloatStrOrBool]] = None,
            resnet_v2: bool = False,
            bottleneck: bool = True,
            channels_per_group: Sequence[int] = (256, 512, 1024, 2048),
            use_projection: Sequence[bool] = (True, True, True, True),
            logits_config: Optional[Mapping[str, Any]] = None,
            name: Optional[str] = None,
            initial_conv_config: Optional[Mapping[str, FloatStrOrBool]] = None,
    ):
        """Constructs a ResNet model.
    Args:
      blocks_per_group: A sequence of length 4 that indicates the number of
        blocks created in each group.
      num_classes: The number of classes to classify the inputs into.
      bn_config: A dictionary of two elements, ``decay_rate`` and ``eps`` to be
        passed on to the :class:`~haiku.BatchNorm` layers. By default the
        ``decay_rate`` is ``0.9`` and ``eps`` is ``1e-5``.
      resnet_v2: Whether to use the v1 or v2 ResNet implementation. Defaults to
        ``False``.
      bottleneck: Whether the block should bottleneck or not. Defaults to
        ``True``.
      channels_per_group: A sequence of length 4 that indicates the number
        of channels used for each block in each group.
      use_projection: A sequence of length 4 that indicates whether each
        residual block should use projection.
      logits_config: A dictionary of keyword arguments for the logits layer.
      name: Name of the module.
      initial_conv_config: Keyword arguments passed to the constructor of the
        initial :class:`~haiku.Conv2D` module.
    """
        super().__init__(name=name)
        self.resnet_v2 = resnet_v2
        self.resnet_v1 = resnet_v1
        self.use_bn = use_bn

        bn_config = dict(bn_config or {})
        bn_config.setdefault("decay_rate", 0.9)
        bn_config.setdefault("eps", 1e-5)
        bn_config.setdefault("create_scale", True)
        bn_config.setdefault("create_offset", True)

        logits_config = dict(logits_config or {})
        logits_config.setdefault("w_init", jnp.zeros)
        logits_config.setdefault("name", "logits")

        # Number of blocks in each group for ResNet.
        check_length(4, blocks_per_group, "blocks_per_group")
        check_length(4, channels_per_group, "channels_per_group")

        initial_conv_config = dict(initial_conv_config or {})
        initial_conv_config.setdefault("output_channels", 64)

        if resnet_v1:
            initial_conv_config.setdefault("kernel_shape", 7)
            initial_conv_config.setdefault("stride", 2)
        else:
            initial_conv_config.setdefault("kernel_shape", 3)
            initial_conv_config.setdefault("stride", 1)

        initial_conv_config.setdefault("with_bias", False)
        initial_conv_config.setdefault("padding", "SAME")
        initial_conv_config.setdefault("name", "initial_conv")

        self.initial_conv = hk.Conv2D(**initial_conv_config)

        if not self.resnet_v2:
            if self.use_bn:
                self.initial_batchnorm = hk.BatchNorm(name="initial_batchnorm",
                                                  **bn_config)

        self.block_groups = []
        strides = (1, 2, 2, 2)
        for i in range(4):
            self.block_groups.append(
                BlockGroup(channels=channels_per_group[i],
                           num_blocks=blocks_per_group[i],
                           stride=strides[i],
                           bn_config=bn_config,
                           resnet_v2=resnet_v2,
                           bottleneck=bottleneck,
                           use_projection=use_projection[i],
                           use_bn=use_bn,
                           name="block_group_%d" % (i)))

        self.logits = hk.Linear(num_classes, **logits_config)

    def __call__(self, inputs, is_training, test_local_stats=False):
        out = inputs
        out = self.initial_conv(out)
        if self.resnet_v1:
            out = hk.max_pool(out,
                              window_shape=(1, 3, 3, 1),
                              strides=(1, 2, 2, 1),
                              padding="SAME")

        for block_group in self.block_groups:
            out = block_group(out, is_training, test_local_stats)
        # out = hk.avg_pool(out, window_shape=4, strides=4, padding='VALID')
        # out = out.reshape(out.shape[0], -1)
        out = jnp.mean(out, axis=(1, 2))
        return self.logits(out)


class ResNet18(ResNet):
    """ResNet18."""

    def __init__(
            self,
            num_classes: int,
            use_bn: bool,
            bn_config: Optional[Mapping[str, FloatStrOrBool]] = None,
            resnet_v1: bool = False,
            resnet_v2: bool = False,
            logits_config: Optional[Mapping[str, Any]] = None,
            name: Optional[str] = None,
            initial_conv_config: Optional[Mapping[str, FloatStrOrBool]] = None,
    ):
        """Constructs a ResNet model.
    Args:
      num_classes: The number of classes to classify the inputs into.
      bn_config: A dictionary of two elements, ``decay_rate`` and ``eps`` to be
        passed on to the :class:`~haiku.BatchNorm` layers.
      resnet_v2: Whether to use the v1 or v2 ResNet implementation. Defaults
        to ``False``.
      logits_config: A dictionary of keyword arguments for the logits layer.
      name: Name of the module.
      initial_conv_config: Keyword arguments passed to the constructor of the
        initial :class:`~haiku.Conv2D` module.
    """
        if resnet_v1:
            CONFIGS = {
                18: {
                    "blocks_per_group": (2, 2, 2, 2),
                    "bottleneck": False,
                    "channels_per_group": (64, 128, 256, 512),
                    "use_projection": (False, True, True, True),
                },
            }
        else:
            CONFIGS = {
                18: {
                    "blocks_per_group": (2, 2, 2, 2),
                    "bottleneck": False,
                    "channels_per_group": (64, 128, 256, 512),
                    "use_projection": (True, True, True, True),
                },
            }
        super().__init__(num_classes=num_classes,
                         use_bn=use_bn,
                         bn_config=bn_config,
                         initial_conv_config=initial_conv_config,
                         resnet_v1=resnet_v1,
                         resnet_v2=resnet_v2,
                         logits_config=logits_config,
                         name=name,
                         **CONFIGS[18])


