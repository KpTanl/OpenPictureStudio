import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from mediapipe.tasks.metadata import schema_py_generated as schema


TASK_DIGEST = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
MODELS = {
    "face_detector.tflite": "b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f",
    "face_landmarks_detector.tflite": "c7d54204ce0448474c7f3fa9af494787c0965cbdd6f20fc72867e43046bd43d5",
}
REVISION = "mediapipe-original-fp32-onnx17-1"


def convert(payload, name):
    model = schema.ModelT.InitFromObj(schema.Model.GetRootAs(payload))
    if len(model.subgraphs) != 1:
        raise ValueError("仅支持原人脸模型的单子图")
    graph = model.subgraphs[0]
    tensors, nodes, initializers, constants = graph.tensors, [], [], {}
    types = {schema.TensorType.FLOAT32: np.dtype("<f4"), schema.TensorType.FLOAT16: np.dtype("<f2"),
             schema.TensorType.INT32: np.dtype("<i4"), schema.TensorType.INT64: np.dtype("<i8")}
    def tensor_name(index):
        return "tensor_" + str(index)
    def add_constant(value, label):
        initializers.append(numpy_helper.from_array(np.asarray(value), label))
        return label
    for index, tensor in enumerate(tensors):
        data = model.buffers[tensor.buffer].data
        if data is not None and len(data):
            if tensor.type not in types:
                raise ValueError("原模型常量类型不受支持：" + str(tensor.type))
            if tensor.sparsity is not None:
                raise ValueError("原模型包含稀疏常量")
            constants[index] = np.frombuffer(data.tobytes(), types[tensor.type]).reshape(tensor.shape)
    def code(op):
        item = model.operatorCodes[op.opcodeIndex]
        return max(item.builtinCode, item.deprecatedBuiltinCode)
    for op in graph.operators:
        if code(op) == schema.BuiltinOperator.DEQUANTIZE:
            if int(op.inputs[0]) not in constants or tensors[int(op.inputs[0])].type != schema.TensorType.FLOAT16:
                raise ValueError("仅支持原权重的 FLOAT16 常量解码")
            constants[int(op.outputs[0])] = constants[int(op.inputs[0])].astype(np.float32)
    used_constants = set()
    def wire(index):
        index = int(index)
        if index in constants and index not in used_constants:
            add_constant(constants[index], tensor_name(index))
            used_constants.add(index)
        return tensor_name(index)
    def operation(kind, inputs, output, **attributes):
        nodes.append(helper.make_node(kind, inputs, [output], name=output, **attributes))
    for index, op in enumerate(graph.operators):
        kind, options = code(op), op.builtinOptions
        if kind == schema.BuiltinOperator.DEQUANTIZE:
            continue
        inputs = [int(v) for v in op.inputs if v >= 0]
        if len(op.outputs) != 1:
            raise ValueError("原模型算子的输出数量不受支持")
        target = int(op.outputs[0])
        output = tensor_name(target)
        activation = getattr(options, "fusedActivationFunction", 0)
        raw = output + "_raw" if activation else output
        if kind in (schema.BuiltinOperator.CONV_2D, schema.BuiltinOperator.DEPTHWISE_CONV_2D):
            weight = constants[inputs[1]].astype(np.float32)
            group = 1
            if kind == schema.BuiltinOperator.DEPTHWISE_CONV_2D:
                group = int(tensors[inputs[0]].shape[-1])
                weight = weight.transpose(3, 0, 1, 2)
            else:
                weight = weight.transpose(0, 3, 1, 2)
            before, after = output + "_nchw_in", output + "_nchw_out"
            operation("Transpose", [wire(inputs[0])], before, perm=[0, 3, 1, 2])
            convolution = [before, add_constant(weight, output + "_weight")]
            if len(inputs) == 3:
                convolution.append(add_constant(constants[inputs[2]].astype(np.float32), output + "_bias"))
            operation("Conv", convolution, after, group=group, kernel_shape=list(weight.shape[-2:]),
                strides=[options.strideH, options.strideW], dilations=[options.dilationHFactor, options.dilationWFactor],
                auto_pad="SAME_UPPER" if options.padding == schema.Padding.SAME else "VALID")
            operation("Transpose", [after], raw, perm=[0, 2, 3, 1])
        elif kind == schema.BuiltinOperator.MAX_POOL_2D:
            before, after = output + "_nchw_in", output + "_nchw_out"
            operation("Transpose", [wire(inputs[0])], before, perm=[0, 3, 1, 2])
            operation("MaxPool", [before], after, kernel_shape=[options.filterHeight, options.filterWidth],
                strides=[options.strideH, options.strideW],
                auto_pad="SAME_UPPER" if options.padding == schema.Padding.SAME else "VALID")
            operation("Transpose", [after], raw, perm=[0, 2, 3, 1])
        elif kind == schema.BuiltinOperator.RESHAPE:
            shape = add_constant(np.asarray(tensors[target].shape, np.int64), output + "_shape")
            operation("Reshape", [wire(inputs[0]), shape], raw)
        elif kind == schema.BuiltinOperator.PAD:
            padding = constants[inputs[1]].astype(np.int64)
            pads = add_constant(np.concatenate((padding[:, 0], padding[:, 1])), output + "_pads")
            operation("Pad", [wire(inputs[0]), pads], raw, mode="constant")
        elif kind == schema.BuiltinOperator.CONCATENATION:
            operation("Concat", [wire(v) for v in inputs], raw, axis=options.axis)
        elif kind in (schema.BuiltinOperator.ADD, schema.BuiltinOperator.RELU,
                      schema.BuiltinOperator.PRELU, schema.BuiltinOperator.LOGISTIC):
            tag = {schema.BuiltinOperator.ADD: "Add", schema.BuiltinOperator.RELU: "Relu",
                   schema.BuiltinOperator.PRELU: "PRelu", schema.BuiltinOperator.LOGISTIC: "Sigmoid"}[kind]
            operation(tag, [wire(v) for v in inputs], raw)
        else:
            raise ValueError("原模型算子不受支持：" + str(kind))
        if activation == schema.ActivationFunctionType.RELU:
            operation("Relu", [raw], output)
        elif activation in (schema.ActivationFunctionType.RELU6, schema.ActivationFunctionType.RELU_N1_TO_1):
            low, high = (0., 6.) if activation == schema.ActivationFunctionType.RELU6 else (-1., 1.)
            operation("Clip", [raw, add_constant(np.float32(low), output+"_min"),
                               add_constant(np.float32(high), output+"_max")], output)
        elif activation:
            raise ValueError("原模型激活函数不受支持：" + str(activation))
    def info(index):
        return helper.make_tensor_value_info(tensor_name(index), TensorProto.FLOAT, [int(v) for v in tensors[index].shape])
    result = helper.make_model(helper.make_graph(nodes, name, [info(int(v)) for v in graph.inputs],
        [info(int(v)) for v in graph.outputs], initializer=initializers),
        producer_name="OpenPictureStudio", producer_version=REVISION,
        opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    helper.set_model_props(result, {"source_sha256": hashlib.sha256(payload).hexdigest(), "conversion": REVISION})
    onnx.checker.check_model(result, full_check=True)
    return result, {"input": [int(v) for v in tensors[int(graph.inputs[0])].shape],
        "outputs": [{"name": tensor_name(int(v)), "shape": [int(n) for n in tensors[int(v)].shape]} for v in graph.outputs]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--locator", type=Path)
    args = parser.parse_args()
    payload = args.source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != TASK_DIGEST:
        raise ValueError("人脸任务包与已审核原权重不一致")
    args.destination.mkdir(parents=True, exist_ok=True)
    manifest = {"revision": REVISION, "source_sha256": TASK_DIGEST}
    with zipfile.ZipFile(args.source) as archive:
        for name, digest in MODELS.items():
            payload = archive.read(name)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("人脸子模型摘要不一致")
            model, record = convert(payload, name)
            destination = args.destination / (Path(name).stem + ".onnx")
            onnx.save_model(model, destination)
            record.update(file=destination.name, sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                          source_sha256=digest, bytes=destination.stat().st_size)
            manifest[Path(name).stem] = record
    (args.destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if args.locator is not None:
        original = args.locator.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4":
            raise ValueError("YuNet 权重与已登记原模型不一致")
        model = onnx.load_model_from_string(original)
        for axis in (2,3):
            dimension = model.graph.input[0].type.tensor_type.shape.dim[axis]
            dimension.ClearField("dim_value")
            dimension.dim_param = "ops_input_" + str(axis)
        for output in model.graph.output:
            dimension = output.type.tensor_type.shape.dim[1]
            dimension.ClearField("dim_value")
            dimension.dim_param = "ops_boxes_" + output.name.rsplit("_",1)[1]
        helper.set_model_props(model, {"source_sha256": digest, "conversion": REVISION})
        onnx.checker.check_model(model, full_check=True)
        destination = args.destination / "face_locator.onnx"
        onnx.save_model(model, destination)
        (args.destination/"locator.json").write_text(json.dumps({"revision": REVISION,
            "source_sha256": digest, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()},indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"destination": str(args.destination.resolve()), "revision": REVISION}, ensure_ascii=False))


if __name__ == "__main__":
    main()
