from dataclasses import dataclass
from typing import List, Optional, Union

import numpy
import torch
import torch.nn as nn
from loguru import logger
from text_generation_server.layers.marlin.util import (
    _check_marlin_kernels,
    marlin_zero_points,
    permute_scales,
    unpack_cols,
)
from text_generation_server.utils.import_utils import SYSTEM
from text_generation_server.utils.kernels import load_kernel
from text_generation_server.utils.log import log_once
from text_generation_server.utils.weights import Weight, Weights, WeightsLoader

if SYSTEM == "cuda":
    quantization = load_kernel(
        module="quantization", repo_id="kernels-community/quantization"
    )
else:
    quantization = None


try:
    major, _minor = torch.cuda.get_device_capability()
    has_sm_8_0 = major >= 8
except Exception:
    has_sm_8_0 = False


GPTQ_MARLIN_BITS = [4, 8]
GPTQ_MARLIN_GROUP_SIZES = [-1, 32, 64, 128]
MARLIN_TILE_SIZE = 16


def can_use_gptq_marlin(
    *, bits: int, groupsize: int, quant_method: str, quantize: str, sym: bool
) -> bool:
    return (
        SYSTEM == "cuda"
        and quantization is not None
        and has_sm_8_0
        and quantize in {"awq", "gptq"}
        and quant_method in {"awq", "gptq"}
        and bits in GPTQ_MARLIN_BITS
        and groupsize in GPTQ_MARLIN_GROUP_SIZES
        # We only support asymmetric quantization for AWQ.
        and (sym or quant_method == "awq")
    )


class GPTQMarlinWeightsLoader(WeightsLoader):
    """
    Loader for using GPTQ- and AWQ-quantized weights with Marlin kernels.
    """

    def __init__(
        self,
        *,
        bits: int,
        desc_act: bool,
        groupsize: int,
        quant_method: str,
        quantize: str,
        sym: bool,
    ):
        self.bits = bits
        self.desc_act = desc_act
        self.groupsize = groupsize
        self.quant_method = quant_method
        self.quantize = quantize
        self.sym = sym

    def get_weights(self, weights: Weights, prefix: str):
        log_once(logger.info, "Using GPTQ-Marlin kernels")
        try:
            qweight = weights.get_tensor(f"{prefix}.qweight")
        except RuntimeError:
            raise RuntimeError(
                f"Cannot load `{self.quantize}` weight for GPTQ -> Marlin repacking, make sure the model is already quantized"
            )

        if not self.sym:
            qzeros = weights.get_tensor(f"{prefix}.qzeros")
        else:
            qzeros = None

        if self.quant_method == "awq":
            g_idx = None
        else:
            g_idx = weights.get_tensor(f"{prefix}.g_idx")
        scales = weights.get_tensor(f"{prefix}.scales")

        return repack_gptq_for_marlin(
            qweight=qweight,
            scales=scales,
            qzeros=qzeros,
            g_idx=g_idx,
            bits=self.bits,
            desc_act=self.desc_act,
            groupsize=self.groupsize,
            quant_method=self.quant_method,
            sym=self.sym,
            sharded_infeatures=False,
        )

    def get_weights_col_packed(
        self,
        weights: Weights,
        prefix: str,
        block_sizes: Union[int, List[int]],
    ):
        try:
            qweight = weights.get_packed_sharded(
                f"{prefix}.qweight", dim=1, block_sizes=block_sizes
            )
        except RuntimeError:
            raise RuntimeError(
                f"Cannot load `{self.quantize}` weight, make sure the model is already quantized."
            )
        scales = weights.get_packed_sharded(
            f"{prefix}.scales", dim=1, block_sizes=block_sizes
        )
        scales = scales.to(dtype=weights.dtype)

        if not self.sym:
            qzeros = weights.get_packed_sharded(
                f"{prefix}.qzeros", dim=1, block_sizes=block_sizes
            )
        else:
            qzeros = None

        if self.quant_method == "awq":
            g_idx = None
        else:
            g_idx = weights.get_tensor(f"{prefix}.g_idx")
        return repack_gptq_for_marlin(
            qweight=qweight,
            scales=scales,
            qzeros=qzeros,
            g_idx=g_idx,
            bits=self.bits,
            desc_act=self.desc_act,
            groupsize=self.groupsize,
            quant_method=self.quant_method,
            sym=self.sym,
            sharded_infeatures=False,
        )

    def get_multi_weights_col(self, weights: Weights, prefixes: List[str], dim: int):
        try:
            qweight = torch.cat(
                [weights.get_sharded(f"{p}.qweight", dim=1) for p in prefixes], dim=1
            )
        except RuntimeError:
            raise RuntimeError(
                f"Cannot load `{self.quantize}` weight, make sure the model is already quantized"
            )

        scales = torch.cat(
            [weights.get_sharded(f"{p}.scales", dim=1) for p in prefixes], dim=1
        )

        if not self.sym:
            qzeros = torch.cat(
                [weights.get_sharded(f"{p}.qzeros", dim=1) for p in prefixes], dim=1
            )
        else:
            qzeros = None

        if self.quant_method == "awq":
            g_idx = None
        else:
            w = [weights.get_tensor(f"{p}.g_idx") for p in prefixes]
            for w2 in w[1:]:
                torch.testing.assert_close(w2, w[0])
            g_idx = w[0]

        return repack_gptq_for_marlin(
            qweight=qweight,
            scales=scales,
            qzeros=qzeros,
            g_idx=g_idx,
            bits=self.bits,
            desc_act=self.desc_act,
            groupsize=self.groupsize,
            quant_method=self.quant_method,
            sym=self.sym,
            sharded_infeatures=False,
        )

    def get_weights_row(self, weights: Weights, prefix: str):
        log_once(logger.info, "Using GPTQ-Marlin kernels")
        try:
            qweight = weights.get_sharded(f"{prefix}.qweight", dim=0)
        except RuntimeError:
            raise RuntimeError(
                f"Cannot load `{self.quantize}` weight for GPTQ -> Marlin repacking, make sure the model is already quantized"
            )

        if not self.sym:
            if self.desc_act or self.groupsize == -1:
                qzeros = weights.get_tensor(f"{prefix}.qzeros")
            else:
                qzeros = weights.get_sharded(f"{prefix}.qzeros", dim=0)
        else:
            qzeros = None

        if self.quant_method == "awq":
            g_idx = None
        else:
            g_idx = weights.get_sharded(f"{prefix}.g_idx", dim=0)

        if self.desc_act or self.groupsize == -1:
            scales = weights.get_tensor(f"{prefix}.scales")
        else:
            scales = weights.get_sharded(f"{prefix}.scales", dim=0)

        sharded_in_features = weights.process_group.size() > 1

        return repack_gptq_for_marlin(
            qweight=qweight,
            scales=scales,
            qzeros=qzeros,
            g_idx=g_idx,
            bits=self.bits,
            desc_act=self.desc_act,
            groupsize=self.groupsize,
            quant_method=self.quant_method,
            sym=self.sym,
            sharded_infeatures=sharded_in_features,
        )

    def _get_gptq_params(self, weights: Weights):
        if weights.has_tensor("gptq_bits") and weights.has_tensor("gptq_groupsize"):
            self.bits = weights.get_tensor("gptq_bits").item()
            self.groupsize = weights.get_tensor("gptq_groupsize").item()
            self.desc_act = False
            # `server quantize` used asymmetric quantization unconditionally
            # before the `gptq_sym` setting tensor was added.
            self.sym = (
                weights.get_tensor("gptq_sym").item()
                if weights.has_tensor("gptq_sym")
                else False
            )
            self.quant_method = "gptq"


@dataclass
class GPTQMarlinWeight(Weight):
    """
    Repacked GPTQ Marlin weights.
    """

    qweight: torch.Tensor
    qzeros: torch.Tensor
    scales: torch.Tensor
    g_idx: torch.Tensor
    perm: torch.Tensor
    bits: int
    is_full_k: bool

    def __post_init__(self):
        assert self.qweight.dtype == torch.int32
        assert self.scales.dtype in (torch.float16, torch.bfloat16)
        assert self.g_idx.dtype == torch.int32
        assert self.perm.dtype == torch.int32

    def get_linear(self, bias: torch.Tensor):
        return GPTQMarlinLinear(
            weight=self,
            bias=bias,
        )


def repack_gptq_for_marlin(
    *,
    qweight: torch.Tensor,
    qzeros: Optional[torch.Tensor],
    scales: torch.Tensor,
    g_idx: Optional[torch.Tensor],
    bits: int,
    desc_act: bool,
    groupsize: int,
    quant_method: str,
    sym: bool,
    sharded_infeatures: bool,
) -> GPTQMarlinWeight:
    """Convert GPTQ weights to a layout that's compatible with GPTQ-Marlin kernels."""
    _check_marlin_kernels()
    assert quantization is not None

    if bits not in GPTQ_MARLIN_BITS:
        supported_bits = ", ".join(str(b) for b in GPTQ_MARLIN_BITS)
        raise RuntimeError(
            f"Repacking {bits}-bit GPTQ weights as Marlin is not supported, must be one of: {supported_bits}"
        )

    if groupsize not in GPTQ_MARLIN_GROUP_SIZES:
        supported_sizes = ", ".join(str(b) for b in GPTQ_MARLIN_GROUP_SIZES)
        raise RuntimeError(
            f"Repacking GPTQ weights with group size {groupsize} as Marlin is not supported, must be one of: {supported_sizes}"
        )
    if not (sym or quant_method == "awq" or quant_method == "compressed-tensors"):
        raise RuntimeError(
            "Repacking GPTQ weights with asymmetric quantization as Marlin is not supported."
        )

    log_once(logger.info, f"Converting {quant_method} model to Marlin packing format.")

    weights_per_int = 32 // bits
    in_features = qweight.shape[0]
    out_features = qweight.shape[1]

    # AWQ uses column packing, GPTQ uses row packing
    if quant_method == "awq":
        out_features *= weights_per_int
    else:
        in_features *= weights_per_int

    if in_features % groupsize != 0:
        raise ValueError(
            f"Number of input features ({in_features}) not divisible by group size ({groupsize})"
        )

    if g_idx is not None and desc_act and groupsize != -1:
        perm = torch.argsort(g_idx).to(torch.int)
        g_idx = g_idx[perm]
    else:
        perm = torch.empty(0, dtype=torch.int, device=qweight.device)
        g_idx = torch.empty(0, dtype=torch.int, device=qweight.device)

    if quant_method == "awq":
        repacked = quantization.awq_marlin_repack(
            qweight, in_features, out_features, bits
        )
        if qzeros is not None:
            qzeros = awq_to_marlin_zero_points(
                qzeros,
                in_features // groupsize,
                out_features,
                bits,
            )

    else:
        repacked = quantization.gptq_marlin_repack(
            qweight, perm, in_features, out_features, bits
        )

    if qzeros is None:
        qzeros = torch.empty(0, dtype=torch.int, device=qweight.device)

    scales = permute_scales(scales)

    is_full_k = not (desc_act and groupsize != -1 and sharded_infeatures)

    return GPTQMarlinWeight(
        qweight=repacked,
        qzeros=qzeros,
        scales=scales,
        g_idx=g_idx,
        perm=perm,
        bits=bits,
        is_full_k=is_full_k,
    )


class GPTQMarlinLinear(nn.Module):
    """
    Linear layer for GPTQ weights that were converted for the GPTQ-Marlin
    kernels.
    """

    def __init__(
        self,
        *,
        weight: GPTQMarlinWeight,
        bias: Optional[torch.Tensor],
    ):
        super().__init__()

        _check_marlin_kernels()
        assert quantization is not None

        in_features = weight.qweight.shape[0] * MARLIN_TILE_SIZE
        out_features = weight.scales.shape[1]
        _check_valid_shape(in_features=in_features, out_features=out_features)

        if weight.bits not in (4, 8):
            raise ValueError("GPTQMarlinLinear only supports 4 and 8-bit quantization")

        if weight.qzeros.numel() > 0:
            if weight.bits == 4:
                self.quant_type = quantization.scalar_types.uint4
            else:
                self.quant_type = quantization.scalar_types.uint8
        else:
            if weight.bits == 4:
                self.quant_type = quantization.scalar_types.uint4b8
            else:
                self.quant_type = quantization.scalar_types.uint8b128

        self.is_full_k = weight.is_full_k

        self.qweight = weight.qweight
        self.qzeros = weight.qzeros
        self.scales = weight.scales
        self.g_idx = weight.g_idx
        self.perm = weight.perm
        if bias is not None:
            self.bias = bias
        else:
            self.bias = None

        self.workspace = torch.zeros(
            out_features // 64 * 16, dtype=torch.int, device=weight.qweight.device
        )

    def forward(self, A: torch.Tensor) -> torch.Tensor:
        assert quantization is not None

        A_flat = A.view(-1, A.shape[-1])
        C = quantization.gptq_marlin_gemm(
            A_flat,
            self.qweight,
            self.scales,
            self.qzeros,
            self.g_idx,
            self.perm,
            self.workspace,
            self.quant_type,
            A_flat.shape[0],
            self.scales.shape[1],
            A_flat.shape[1],
            self.is_full_k,
            self.qzeros.numel() > 0,
            True,
        )
        C = C.reshape(A.shape[:-1] + (self.scales.shape[1],))

        if self.bias is not None:
            C += self.bias

        return C


def awq_to_marlin_zero_points(
    q_zp_packed: torch.Tensor, size_k: int, size_n: int, num_bits: int
) -> torch.Tensor:
    # AWQ zero-points are quantized and packed on the column dim.
    # In addition, the values are permuted based on dequantizer.
    # Here we undo both of these, and then apply marlin permutation
    # and pack it back.
    q_zp = unpack_cols(q_zp_packed, num_bits, size_k, size_n)

    # Undo interleaving (use argsort(..) to get inverse perm)
    if num_bits == 4:
        undo_interleave = numpy.argsort(numpy.array([0, 2, 4, 6, 1, 3, 5, 7]))
    elif num_bits == 8:
        undo_interleave = numpy.argsort(numpy.array([0, 2, 1, 3]))
    else:
        raise Exception("num_bits must be 4 or 8, got {}".format(num_bits))

    q_zp = q_zp.reshape((-1, len(undo_interleave)))[:, undo_interleave].ravel()
    q_zp = q_zp.reshape((-1, size_n)).contiguous()

    marlin_zp = marlin_zero_points(q_zp, size_k, size_n, num_bits)
    return marlin_zp


def _check_valid_shape(in_features: int, out_features: int):
    if (in_features % 128 != 0 or out_features % 64 != 0) and (
        in_features % 64 != 0 or out_features % 128 != 0
    ):
        raise ValueError(
            f"The GPTQ Marlin kernel does not have a valid thread configuration for weight matrix with shape ({out_features}, {in_features})."
            " The shape elements must be divisible by (128, 64) or (64, 128)."
        )
