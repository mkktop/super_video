"""uint8 直进直出图手术：把每帧 CPU 前后处理塞进 ONNX 图，全在 GPU 执行。

背景（BENCH.md 2026-08-25）：engine.process() 每帧 ~100ms 花在 CPU numpy——
输入 HWC→NCHW 转置 + ÷255，输出 4K 95MB fp32 的 ×255/clip/转 uint8；GPU 裸推理
只占 45-61ms。图手术包装后 138→36.7ms/帧（3.8x），输出与原路径逐位一致
（fp16 转换模型还跳过了一次输出 fp16 往返量化，保真只高不低）。

包装结构（io 约定自适应）：
  输入 uint8[N,H,W,3] → Cast f32 → (bgr: Gather 通道反转) → Transpose NCHW
    → (0-1: Div 255) → (fp16 模型: Cast f16) → 原模型输入
  原模型输出 → (绕过末端 fp32→fp16 Cast，直接取 fp32) → (0-1: Mul 255)
    → Clip 0-255 → Transpose NHWC → Cast uint8 → (bgr: Gather 通道反转) → 输出

DML 落地坑（bench 实测）：
- fp16 转换模型内部 fp32→fp16 Cast 作为末端输出节点时不真正执行（走拷贝路径），
  被追加节点消费即崩 0x8007023E——必须绕过末端 Cast 取其 fp32 输入；
- 包装 session 用 ORT_ENABLE_BASIC（全量优化会重排被输出边界保护的算子触发上坑）。
"""
from __future__ import annotations

from pathlib import Path

_F32 = 1  # TensorProto.FLOAT
_F16 = 10  # TensorProto.FLOAT16


def wrap_u8(src: Path, dst: Path, *, color: str = "rgb", range_01: bool = True) -> None:
    """生成 uint8 直进直出包装模型；不适用时抛异常（调用方回退原路径）。"""
    import onnx
    from onnx import TensorProto, helper

    if color not in ("rgb", "bgr"):
        raise ValueError(f"不支持的通道约定 {color}")
    m = onnx.load(str(src))
    g = m.graph
    opset = next((o.version for o in m.opset_import if o.domain in ("", "ai.onnx")), 0)
    if opset < 11:
        raise ValueError(f"opset {opset} < 11（Clip 输入式/Mul 标量需 11+）")
    if len(g.output) != 1:
        raise ValueError("多输出模型不支持")

    old_in = g.input[0]
    inner_dtype = old_in.type.tensor_type.elem_type
    if inner_dtype not in (_F32, _F16):
        raise ValueError(f"输入 dtype {inner_dtype} 非 float")
    out_dtype = g.output[0].type.tensor_type.elem_type
    sfx = "u8w"

    # ---- 输出侧：绕过/改名原输出，追加 ×255/Clip/转置/Cast 尾链 ----
    old_out_name = g.output[0].name
    tail_node = next((n for n in g.node if old_out_name in n.output), None)
    bypassed = (tail_node is not None and tail_node.op_type == "Cast"
                and out_dtype == TensorProto.FLOAT16)
    if bypassed:
        raw_out = tail_node.input[0]  # 末端 f32→f16 Cast：绕过取 fp32（DML 崩溃核）
        g.node.remove(tail_node)
    else:
        raw_out = f"{old_out_name}_raw"
        for n in g.node:
            n.output[:] = [raw_out if o == old_out_name else o for o in n.output]
    g.output.remove(g.output[0])

    nodes = []
    post_src = raw_out
    if not bypassed and out_dtype == TensorProto.FLOAT16:
        nodes.append(helper.make_node("Cast", [raw_out], [f"pf32_{sfx}"], to=_F32))
        post_src = f"pf32_{sfx}"
    if range_01:
        nodes.append(helper.make_node("Mul", [post_src, f"c255f_{sfx}"], [f"scaled_{sfx}"]))
        clipped_src = f"scaled_{sfx}"
    else:
        clipped_src = post_src
    nodes.append(helper.make_node("Clip", [clipped_src, f"c0_{sfx}", f"c255u_{sfx}"],
                                  [f"clipped_{sfx}"]))
    nodes.append(helper.make_node("Transpose", [f"clipped_{sfx}"], [f"hwc_{sfx}"],
                                  perm=[0, 2, 3, 1]))
    nodes.append(helper.make_node("Cast", [f"hwc_{sfx}"], [f"ou8_{sfx}"],
                                  to=TensorProto.UINT8))
    if color == "bgr":
        nodes.append(helper.make_node("Gather", [f"ou8_{sfx}", f"flip_{sfx}"],
                                      ["out_u8"], axis=3))

    # ---- 输入侧：原输入改为由前置链生产（uint8 NHWC 直进） ----
    old_in_name = old_in.name
    old_in.name = "inner_in"
    for n in g.node:
        n.input[:] = ["inner_in" if i == old_in_name else i for i in n.input]
    g.input.remove(old_in)  # SSA：不能同时是图输入和节点输出
    g.input.insert(0, helper.make_tensor_value_info(
        "frame_u8", TensorProto.UINT8, ["n", "h", "w", 3]))

    pre = [helper.make_node("Cast", ["frame_u8"], [f"i32_{sfx}"], to=_F32)]
    if color == "bgr":
        pre.append(helper.make_node("Gather", [f"i32_{sfx}", f"flip_{sfx}"],
                                    [f"iflip_{sfx}"], axis=3))
        after_flip = f"iflip_{sfx}"
    else:
        after_flip = f"i32_{sfx}"
    pre.append(helper.make_node("Transpose", [after_flip], [f"inchw_{sfx}"], perm=[0, 3, 1, 2]))
    if range_01:
        pre.append(helper.make_node("Div", [f"inchw_{sfx}", f"c255f_{sfx}"], [f"normed_{sfx}"]))
        normed = f"normed_{sfx}"
    else:
        normed = f"inchw_{sfx}"
    if inner_dtype == _F16:
        pre.append(helper.make_node("Cast", [normed], ["inner_in"], to=_F16))
    else:
        pre[-1].output[0] = "inner_in"
    for i, n in enumerate(pre):
        g.node.insert(i, n)  # 前置链必须插表头（拓扑序）
    g.node.extend(nodes)

    out_name = "out_u8" if color == "bgr" else f"ou8_{sfx}"
    g.output.append(helper.make_tensor_value_info(out_name, TensorProto.UINT8,
                                                  ["n", "h", "w", 3]))
    f32 = TensorProto.FLOAT
    inits = [
        helper.make_tensor(f"c255f_{sfx}", f32, [], [255.0]),
        helper.make_tensor(f"c0_{sfx}", f32, [], [0.0]),
        helper.make_tensor(f"c255u_{sfx}", f32, [], [255.0]),
        helper.make_tensor(f"flip_{sfx}", TensorProto.INT64, [3], [2, 1, 0]),
    ]
    existing = {i.name for i in g.initializer}
    for t in inits:
        if t.name in existing:
            raise ValueError(f"初始化器名冲突 {t.name}")
        g.initializer.append(t)

    onnx.checker.check_model(m)
    dst.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(m, str(dst))
