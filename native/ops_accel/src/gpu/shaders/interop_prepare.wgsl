@group(0) @binding(0) var<storage, read> source: array<vec4<f32>>;
@group(0) @binding(1) var<storage, read_write> tensor_input: array<vec4<f32>>;
@group(0) @binding(2) var<storage, read_write> tensor_output: array<vec4<f32>>;
@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if (id.x >= arrayLength(&source)) { return; }
    tensor_input[id.x] = source[id.x] - vec4<f32>(0.25);
    tensor_output[id.x] = vec4<f32>(0.0);
}
