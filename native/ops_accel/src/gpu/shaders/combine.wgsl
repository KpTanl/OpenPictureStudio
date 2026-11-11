
struct Params { w: i32, h: i32, ow: i32, oh: i32, r: i32, eps: f32, bx: i32, by: i32, bw: i32, bh: i32, pad0: i32, pad1: i32 };

@group(0) @binding(0) var<storage, read> guide: array<f32>;
@group(0) @binding(1) var<storage, read> a: array<f32>;
@group(0) @binding(2) var<storage, read> b: array<f32>;
@group(0) @binding(3) var<storage, read_write> d: array<f32>;
@group(0) @binding(4) var<uniform> p: Params;
@compute @workgroup_size(64) fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = i32(id.x); let y = i32(id.y);
    if (x >= p.w) { return; }
    let i = y * p.w + x;
    d[i] = a[i] * guide[i] + b[i];
}
