# mypy: allow-untyped-defs
import logging
from dataclasses import dataclass
from typing import Any, Optional

import torch
from torch._dynamo.utils import counters
from torch._inductor.virtualized import V
from torch.utils._triton import has_triton_tma_device

from ..ir import ChoiceCaller, Layout, TensorBox
from ..lowering import register_lowering
from ..runtime.runtime_utils import next_power_of_2
from ..select_algorithm import (
    autotune_select_algorithm,
    ExternKernelChoice,
    realize_inputs,
    TritonTemplate,
)
from ..utils import get_gpu_shared_memory, get_num_sms, use_aten_gemm_kernels
from .mm_common import (
    _is_static_problem,
    check_supported_striding,
    persistent_grouped_mm_grid,
)


log = logging.getLogger(__name__)
aten = torch.ops.aten


@dataclass
class Config:
    kwargs: dict[str, int]
    num_stages: int
    num_warps: int


_NV_CONFIGS = [
    Config(
        {
            "BLOCK_M": block_size_m,
            "BLOCK_N": block_size_n,
            "BLOCK_K": block_size_k,
            "NUM_CONSUMER_GROUPS": 1,
        },
        num_stages=num_stages,
        num_warps=num_warps,
    )
    for block_size_m in [64, 128]
    for block_size_n in [64, 128, 256]
    for block_size_k in [64, 128, 256]
    for num_stages in [3, 4]
    for num_warps in [4, 8]
]

_AMD_CONFIGS = [
    Config(
        {
            "BLOCK_M": block_size_m,
            "BLOCK_N": block_size_n,
            "BLOCK_K": block_size_k,
            "waves_per_eu": waves_per_cu,
            "matrix_instr_nonkdim": matrix_instr_nonkdim,
            "NUM_CONSUMER_GROUPS": 1,
        },
        num_stages=num_stages,
        num_warps=num_warps,
    )
    for block_size_m in [32, 64, 128]
    for block_size_n in [32, 64, 128, 256]
    for block_size_k in [128, 256]
    for num_stages in [1, 2]
    for num_warps, waves_per_cu in [(4, 1), (8, 2), (16, 4)]
    for matrix_instr_nonkdim in [16]
]


def scaled_grouped_mm_configs():
    return _AMD_CONFIGS if torch.version.hip else _NV_CONFIGS


def early_config_prune(configs, named_args):
    dtsize = 1
    pruned_configs = []
    for config in configs:
        kw = config.kwargs
        BLOCK_M, BLOCK_N, BLOCK_K, num_stages, num_warps, num_consumer_groups = (
            kw["BLOCK_M"],
            kw["BLOCK_N"],
            kw["BLOCK_K"],
            config.num_stages,
            config.num_warps,
            getattr(config, "num_consumer_groups", 0),
        )
        G, M, N, M_IS_DYNAMIC, N_IS_DYNAMIC = (
            named_args["G"],
            named_args["M"],
            named_args["N"],
            named_args["M_IS_DYNAMIC"],
            named_args["N_IS_DYNAMIC"],
        )
        M_PER_GROUP = next_power_of_2(M) // G if M_IS_DYNAMIC else M
        N_PER_GROUP = next_power_of_2(N) // G if N_IS_DYNAMIC else N

        # 1. make sure we have enough smem
        max_shared_memory = get_gpu_shared_memory()

        if torch.version.hip:
            required_shared_memory = BLOCK_N * BLOCK_K * num_stages * dtsize
        else:
            required_shared_memory = (BLOCK_M + BLOCK_N) * BLOCK_K * num_stages * dtsize
        if required_shared_memory > max_shared_memory:
            continue

        use_warp_specialization = num_consumer_groups >= 1

        MIN_M_TILES = 32 if torch.version.hip else 64
        # 2. make sure we don't load M tiles that are too big
        if (
            not use_warp_specialization
            and BLOCK_M > MIN_M_TILES
            and BLOCK_M > (M_PER_GROUP * 2)
        ):
            continue
        # 3. make sure we don't load M tiles that are too small
        if BLOCK_M < 128 and BLOCK_M < (M_PER_GROUP // 2):
            continue

        num_sm = get_num_sms()

        N_TILES = N_PER_GROUP // BLOCK_N
        MIN_N_TILES = 32 if torch.version.hip else 64
        # 4. make sure we don't load N tiles that are too big
        if (
            not use_warp_specialization
            and BLOCK_N > MIN_N_TILES
            and G * M_PER_GROUP * N_TILES < num_sm
        ):
            continue
        # 5. make sure we don't load N tiles that are too small
        if BLOCK_N < 128 and G * M_PER_GROUP * N_TILES > 2 * num_sm:
            continue

        # 6. make sure we can partition for ws
        if use_warp_specialization:
            if num_warps != 4:
                continue

            # "tritongpu-warp-spec-data-partition"
            m_slice = BLOCK_M // num_consumer_groups
            n_slice = BLOCK_N // num_consumer_groups
            if m_slice < 64 and n_slice < 256:
                continue

        pruned_configs.append(config)

    return pruned_configs


# Copied from fbgemm grouped_gemm.py
triton_scaled_grouped_mm_source = r"""
{% if M_IS_DYNAMIC or N_IS_DYNAMIC or K_IS_DYNAMIC %}
{{def_kernel("a_ptr", "b_ptr", "scale_a_ptr", "scale_b_ptr", "offsets_ptr")}}
{% else %}
{{def_kernel("a_ptr", "b_ptr", "scale_a_ptr", "scale_b_ptr")}}
{% endif %}
    tidx = tl.program_id(0)

    a_desc = tl._experimental_make_tensor_descriptor(
        a_ptr,
{% if A_IS_2D %}
        shape=[A_SIZE_M, A_SIZE_K],
        strides=[A_STRIDE_M, A_STRIDE_K],
        block_shape=[BLOCK_M, BLOCK_K],
{% else %}
        shape=[A_SIZE_G, A_SIZE_M, A_SIZE_K],
        strides=[A_STRIDE_G, A_STRIDE_M, A_STRIDE_K],
        block_shape=[1, BLOCK_M, BLOCK_K],
{% endif %}
    )
    b_desc = tl._experimental_make_tensor_descriptor(
        b_ptr,
{% if B_IS_2D %}
        shape=[B_SIZE_N, B_SIZE_K],
        strides=[B_STRIDE_N, B_STRIDE_K],
        block_shape=[BLOCK_N, BLOCK_K],
{% else %}
        shape=[B_SIZE_G, B_SIZE_N, B_SIZE_K],
        strides=[B_STRIDE_G, B_STRIDE_N, B_STRIDE_K],
        block_shape=[1, BLOCK_N, BLOCK_K],
{% endif %}
    )

{% if M_IS_DYNAMIC %}
    m_end_offset = 0
{% endif %}
{% if N_IS_DYNAMIC %}
    n_end_offset = 0
{% endif %}
{% if K_IS_DYNAMIC %}
    k_end_offset = 0
{% endif %}
    iterated_tiles = 0
    for g in tl.range(G):
{% if M_IS_DYNAMIC %}
        # Move across groups
        m_start_offset = m_end_offset
        m_end_offset = tl.load(offsets_ptr + g)
        m_size = m_end_offset - m_start_offset
        m_scale_start_offset = m_start_offset
{% else %}
        m_start_offset = 0
        m_size = M
{% if A_IS_2D %}
        m_scale_start_offset = g.to(tl.int64) * M
{% endif %}
{% endif %}

        if m_size > 0:
{% if N_IS_DYNAMIC %}
            # Move across groups
            n_start_offset = n_end_offset
            n_end_offset = tl.load(offsets_ptr + g)
            n_size = n_end_offset - n_start_offset
            n_scale_start_offset = n_start_offset
{% else %}
            n_start_offset = 0
            n_size = N
{% if B_IS_2D %}
            n_scale_start_offset = g.to(tl.int64) * N
{% endif %}
{% endif %}
{% if K_IS_DYNAMIC %}
            # Move across groups
            k_start_offset = k_end_offset
            k_end_offset = tl.load(offsets_ptr + g)
            k_size = k_end_offset - k_start_offset
{% else %}
            k_start_offset = 0
            k_size = K
{% endif %}

            num_m_tiles = tl.cdiv(m_size, BLOCK_M)
            num_n_tiles = tl.cdiv(n_size, BLOCK_N)
            num_tiles = num_m_tiles * num_n_tiles

            # Move across tiles
            while tidx >= iterated_tiles and tidx < iterated_tiles + num_tiles:
                gidx = tidx - iterated_tiles
                # Split M first and N second.
                tile_m_idx = gidx % num_m_tiles
                tile_n_idx = gidx // num_m_tiles

                accumulator = tl.zeros((BLOCK_M, BLOCK_N), dtype=tl.float32)

{% if USE_TMA_LOAD %}
                m_offset = (m_start_offset + tile_m_idx * BLOCK_M).to(tl.int32)
                n_offset = (n_start_offset + tile_n_idx * BLOCK_N).to(tl.int32)

                for k_offset in range(0, k_size, BLOCK_K):
{% if A_IS_2D %}
                    a = a_desc.load([m_offset, k_start_offset + k_offset])
{% else %}
                    a = a_desc.load([g, m_offset, k_start_offset + k_offset]).reshape(BLOCK_M, BLOCK_K)
{% endif %}
{% if B_IS_2D %}
                    b = b_desc.load([n_offset, k_start_offset + k_offset])
{% else %}
                    b = b_desc.load([g, n_offset, k_start_offset + k_offset]).reshape(BLOCK_N, BLOCK_K)
{% endif %}

{% if K_IS_DYNAMIC %}
                    if k_offset + BLOCK_K > k_size:
                        group_offs_k = k_offset + tl.arange(0, BLOCK_K)
                        a = tl.where(group_offs_k < k_size, a, 0)
                        b = tl.where(group_offs_k < k_size, b, 0)
{% endif %}

{% if USE_FAST_ACCUM %}
                    accumulator = tl.dot(a, b.T, accumulator)
{% else %}
                    accumulator += tl.dot(a, b.T)
{% endif %}
{% else %}
                offs_am = tile_m_idx * BLOCK_M + tl.arange(0, BLOCK_M)
                offs_bn = tile_n_idx * BLOCK_N + tl.arange(0, BLOCK_N)
                offs_k = k_start_offset + tl.arange(0, BLOCK_K)
                a_ptrs = (
                    a_ptr
{% if not A_IS_2D %}
                    + g * A_STRIDE_G
{% endif %}
                    + (m_start_offset + offs_am[:, None]) * A_STRIDE_M
                    + offs_k[None, :] * A_STRIDE_K
                )
                b_ptrs = (
                    b_ptr
{% if not B_IS_2D %}
                    + g * B_STRIDE_G
{% endif %}
                    + (n_start_offset + offs_bn[:, None]) * B_STRIDE_N
                    + offs_k[None, :] * B_STRIDE_K
                )
                for k_offset in range(0, k_size, BLOCK_K):
                    a = tl.load(a_ptrs, mask=offs_am[:, None] < m_size)
                    b = tl.load(b_ptrs, mask=offs_bn[:, None] < n_size)
                    if k_offset + BLOCK_K > k_size:
                        group_offs_k = k_offset + tl.arange(0, BLOCK_K)
                        a = tl.where(group_offs_k < k_size, a, 0)
                        b = tl.where(group_offs_k < k_size, b, 0)
{% if USE_FAST_ACCUM %}
                    accumulator = tl.dot(a, b.T, accumulator)
{% else %}
                    accumulator += tl.dot(a, b.T)
{% endif %}
                    a_ptrs += BLOCK_K
                    b_ptrs += BLOCK_K
{% endif %}

                offs_am = tile_m_idx * BLOCK_M + tl.arange(0, BLOCK_M)
                offs_bn = tile_n_idx * BLOCK_N + tl.arange(0, BLOCK_N)
                scale_a = tl.load(
                    scale_a_ptr
{% if A_IS_2D %}
                    + m_scale_start_offset
{% else %}
                    + g * SCALE_A_STRIDE_G
{% endif %}
                    + offs_am[:, None],
                    mask=offs_am[:, None] < m_size,
                )
                scale_b = tl.load(
                    scale_b_ptr
{% if B_IS_2D %}
                    + n_scale_start_offset
{% else %}
                    + g * SCALE_B_STRIDE_G
{% endif %}
                    + offs_bn[None, :],
                    mask=offs_bn[None, :] < n_size,
                )
                c = accumulator.to(tl.float32) * scale_a * scale_b

{% if M_IS_DYNAMIC %}
                idx_m = (m_start_offset + offs_am[:, None])
{% else %}
                idx_m = offs_am[:, None]
{% endif %}
{% if N_IS_DYNAMIC %}
                idx_n = (n_start_offset + offs_bn[None, :])
{% else %}
                idx_n = offs_bn[None, :]
{% endif %}

                mask = offs_am[:, None] < m_size and offs_bn[None, :] < n_size

{% if M_IS_DYNAMIC or N_IS_DYNAMIC %}
                {{store_output(("idx_m", "idx_n"), "c", "mask", indent_width=16)}}
{% else %}
                {{store_output(("g", "idx_m", "idx_n"), "c", "mask", indent_width=16)}}
{% endif %}
                tidx += NUM_SMS

            iterated_tiles += num_tiles
"""


triton_scaled_grouped_mm_template = TritonTemplate(
    name="scaled_grouped_mm",
    grid=persistent_grouped_mm_grid,
    source=triton_scaled_grouped_mm_source,
)


def grouped_mm_args(
    mat1: TensorBox,
    mat2: TensorBox,
    offs: Optional[TensorBox],
    layout=None,
    out_dtype=None,
):
    mat1, mat2 = realize_inputs(mat1, mat2)
    if offs is not None:
        realize_inputs(offs)
    mat1_size = mat1.get_size()
    mat2_size = mat2.get_size()

    m1dim, m2dim = len(mat1_size), len(mat2_size)

    assert m1dim == 2 or m1dim == 3
    assert m2dim == 2 or m2dim == 3

    if layout is None:
        from torch._inductor.ir import FixedLayout

        if out_dtype is None:
            out_dtype = mat1.get_dtype()

        dims = []
        if m1dim == 2:
            if m2dim == 2:
                assert offs is not None
                dims = [offs.get_size()[0], mat1_size[0], mat2_size[1]]
            else:
                dims = [mat1_size[0], mat2_size[-1]]
        else:
            if m2dim == 2:
                dims = [mat1_size[1], mat2_size[1]]
            else:
                dims = [mat1_size[0], mat1_size[1], mat2_size[-1]]
        layout = FixedLayout(
            mat1.get_device(),
            out_dtype,
            dims,
        )
    else:
        assert out_dtype is None, "out_dtype is ignored if layout is specified."

    return (mat1_size, mat2_size, layout, mat1, mat2, offs)


aten__scaled_grouped_mm = ExternKernelChoice(
    torch._scaled_grouped_mm,
    "at::_scaled_grouped_mm",
    op_overload=aten._scaled_grouped_mm,
    has_out_variant=False,
)


def can_use_triton_kernel(
    mat_a: TensorBox,
    mat_b: TensorBox,
    offs: Optional[TensorBox],
    bias: Optional[TensorBox],
) -> bool:
    if not has_triton_tma_device():
        return False

    # The _scaled_grouped_mm() operator doesn't support bias yet.
    if bias is not None:
        return False

    m1_size = mat_a.get_size()
    m2_size = mat_b.get_size()

    if len(m1_size) == 2:
        if len(m2_size) == 2:
            return offs is not None and m2_size[-1] >= 32
        else:
            return offs is not None and m1_size[-1] >= 32 and m2_size[-2] >= 32
    else:
        if len(m2_size) == 2:
            return offs is not None and m2_size[-2] >= 32
        else:
            return offs is None and m1_size[-1] >= 32 and m2_size[-1] >= 32


@register_lowering(aten._scaled_grouped_mm.default, type_promotion_kind=None)
def tuned_scaled_grouped_mm(
    mat_a: TensorBox,
    mat_b: TensorBox,
    scale_a: TensorBox,
    scale_b: TensorBox,
    offs: Optional[TensorBox] = None,
    bias: Optional[TensorBox] = None,
    scale_result: Optional[TensorBox] = None,
    out_dtype: Optional[torch.dtype] = None,
    use_fast_accum: bool = False,
    layout: Optional[Layout] = None,
) -> TensorBox:
    """Auto-tuning for _scaled_grouped_mm() operator."""

    m1_size, m2_size, layout, mat_a, mat_b, offs = grouped_mm_args(
        mat_a, mat_b, offs, layout=layout, out_dtype=out_dtype
    )
    counters["aten_mm_info"]["aten._scaled_grouped_mm.default"] += 1
    log.info(
        "Tuned aten._scaled_grouped_mm.default: mat1_shape=%s, mat2_shape=%s, mat1_dtype=%s, mat2_dtype=%s, output_layout=%s",
        m1_size,
        m2_size,
        mat_a.get_dtype(),
        mat_b.get_dtype(),
        layout,
    )
    check_supported_striding(mat_a, mat_b)

    scale_a, scale_b = realize_inputs(scale_a, scale_b)

    # workaround for Inductor not supporting optional tensor input arguments
    input_nodes: list[Any] = [mat_a, mat_b, scale_a, scale_b]
    if offs is not None:
        input_nodes.append(realize_inputs(offs))
    if bias is not None:
        input_nodes.append(realize_inputs(bias))

    aten_choice = aten__scaled_grouped_mm.bind(
        input_nodes,
        layout,
        out_dtype=out_dtype,
        use_fast_accum=use_fast_accum,
    )

    choices: list[ChoiceCaller] = []
    if use_aten_gemm_kernels():
        choices.append(aten_choice)

    _, is_nonzero = _is_static_problem(layout)

    if is_nonzero and can_use_triton_kernel(mat_a, mat_b, offs, bias):
        if len(m1_size) == 2:
            if len(m2_size) == 2:
                g = offs.layout.size[0]
                m, k1 = m1_size
                k2, n = m2_size
                k = V.graph.sizevars.guard_equals(k1, k2)
                m_is_dynamic, n_is_dynamic, k_is_dynamic = False, False, True
            else:
                m, k1 = m1_size
                g, k2, n = m2_size
                k = V.graph.sizevars.guard_equals(k1, k2)
                m_is_dynamic, n_is_dynamic, k_is_dynamic = True, False, False
        else:
            if len(m2_size) == 2:
                g, m, k1 = m1_size
                k2, n = m2_size
                k = V.graph.sizevars.guard_equals(k1, k2)
                m_is_dynamic, n_is_dynamic, k_is_dynamic = False, True, False
            else:
                g1, m, k1 = m1_size
                g2, k2, n = m2_size
                g = V.graph.sizevars.guard_equals(g1, g2)
                k = V.graph.sizevars.guard_equals(k1, k2)
                m_is_dynamic, n_is_dynamic, k_is_dynamic = False, False, False

        kwargs = {
            "G": g,
            "M": m,
            "N": n,
            "K": k,
            "A_IS_2D": len(m1_size) == 2,
            "B_IS_2D": len(m2_size) == 2,
            "M_IS_DYNAMIC": m_is_dynamic,
            "N_IS_DYNAMIC": n_is_dynamic,
            "K_IS_DYNAMIC": k_is_dynamic,
            "NUM_SMS": get_num_sms(),
            "USE_TMA_LOAD": True,
            "USE_FAST_ACCUM": use_fast_accum,
        }

        a_size = mat_a.get_size()
        b_size = mat_b.get_size()
        a_stride = mat_a.get_stride()
        b_stride = mat_b.get_stride()
        scale_a_stride = scale_a.get_stride()
        scale_b_stride = scale_b.get_stride()
        kwargs["A_SIZE_M"], kwargs["A_SIZE_K"] = a_size[-2], a_size[-1]
        kwargs["A_STRIDE_M"], kwargs["A_STRIDE_K"] = a_stride[-2], a_stride[-1]
        if len(a_size) == 3:
            kwargs["A_SIZE_G"] = a_size[0]
            kwargs["A_STRIDE_G"] = a_stride[0]
            kwargs["SCALE_A_STRIDE_G"] = scale_a_stride[0]
        # the b_mat is given with its last two dims transposed, revert here
        kwargs["B_SIZE_N"], kwargs["B_SIZE_K"] = b_size[-1], b_size[-2]
        kwargs["B_STRIDE_N"], kwargs["B_STRIDE_K"] = b_stride[-1], b_stride[-2]
        if len(b_size) == 3:
            kwargs["B_SIZE_G"] = b_size[0]
            kwargs["B_STRIDE_G"] = b_stride[0]
            kwargs["SCALE_B_STRIDE_G"] = scale_b_stride[0]

        for config in early_config_prune(scaled_grouped_mm_configs(), kwargs):
            triton_scaled_grouped_mm_template.maybe_append_choice(
                choices,
                input_nodes=input_nodes,
                layout=layout,
                num_stages=config.num_stages,
                num_warps=config.num_warps,
                **kwargs,
                **config.kwargs,
            )

    if has_triton_tma_device():
        # TMA descriptors require a global memory allocation
        def alloc_fn(size: int, alignment: int, stream: Optional[int]):
            return torch.empty(size, device=mat_a.get_device(), dtype=torch.int8)

        import triton

        triton.set_allocator(alloc_fn)

    return autotune_select_algorithm("scaled_grouped_mm", choices, input_nodes, layout)
