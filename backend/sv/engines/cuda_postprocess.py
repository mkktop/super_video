"""CUDA output conversion without changing the model's TensorRT graph."""
from __future__ import annotations


def create_session(ort, output_type: str, *, color: str, range_01: bool,
                   affine: tuple | None = None):
    import onnx
    from onnx import TensorProto as T, helper as h

    dtype = {"tensor(float)": T.FLOAT, "tensor(float16)": T.FLOAT16}.get(output_type)
    if dtype is None or color not in ("rgb", "bgr"):
        raise ValueError(f"CUDA 后处理不支持 {output_type}/{color}")
    nodes, constants = [], []
    value = "raw"

    def scalar(name, value):
        constants.append(h.make_tensor(name, T.FLOAT, [], [float(value)]))
        return name

    if dtype == T.FLOAT16:
        nodes.append(h.make_node("Cast", [value], ["fp32"], to=T.FLOAT))
        value = "fp32"
    if affine:
        a, b = affine
        nodes.append(h.make_node("Sub", [value, scalar("offset", b)], ["shifted"]))
        nodes.append(h.make_node("Div", ["shifted", scalar("factor", a)], ["unscaled"]))
        value = "unscaled"
    if range_01:
        nodes.append(h.make_node("Mul", [value, scalar("c255", 255)], ["scaled"]))
        value = "scaled"
    nodes.append(h.make_node("Clip", [value, scalar("lo", 0), scalar("hi", 255)], ["clipped"]))
    nodes.append(h.make_node("Transpose", ["clipped"], ["hwc"], perm=[0, 2, 3, 1]))
    value = "hwc"
    if color == "bgr":
        constants.append(h.make_tensor("channels", T.INT64, [3], [2, 1, 0]))
        nodes.append(h.make_node("Gather", [value, "channels"], ["rgb"], axis=3))
        value = "rgb"
    nodes.append(h.make_node("Cast", [value], ["pixels"], to=T.UINT8))
    graph = h.make_graph(
        nodes, "cuda_sr_output",
        [h.make_tensor_value_info("raw", dtype, ["n", 3, "h", "w"])],
        [h.make_tensor_value_info("pixels", T.UINT8, ["n", "h", "w", 3])],
        constants,
    )
    model = h.make_model(graph, opset_imports=[h.make_opsetid("", 17)])
    model.ir_version = 10
    onnx.checker.check_model(model)
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.intra_op_num_threads = 1
    session = ort.InferenceSession(
        model.SerializeToString(), options,
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    if session.get_providers()[:1] != ["CUDAExecutionProvider"]:
        raise RuntimeError("CUDA 后处理会话未启用 GPU")
    return session


def run(session, input_name: str, output_name: str, tensor, post_session):
    # The original model executes unchanged. Only the final uint8 pixels cross
    # back to host memory; the larger floating-point output stays on the GPU.
    binding = session.io_binding()
    binding.bind_cpu_input(input_name, tensor)
    binding.bind_output(output_name, "cuda")
    session.run_with_iobinding(binding)
    raw = binding.get_outputs()[0]
    post_binding = post_session.io_binding()
    post_binding.bind_ortvalue_input("raw", raw)
    post_binding.bind_output("pixels", "cuda")
    post_session.run_with_iobinding(post_binding)
    return post_binding.copy_outputs_to_cpu()[0][0]
