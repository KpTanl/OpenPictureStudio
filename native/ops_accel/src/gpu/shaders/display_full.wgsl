
struct Params { w: i32, h: i32, ow: i32, oh: i32, r: i32, eps: f32, bx: i32, by: i32, bw: i32, bh: i32, pad0: i32, pad1: i32 };

fn oetf(v0: f32) -> f32 {
    let v = clamp(v0, 0.0, 1.0);
    if (v <= 0.0031308) { return 12.92 * v; }
    return 1.055 * pow(v, 1.0 / 2.4) - 0.055;
}

@group(0) @binding(0) var<storage, read> s: array<f32>;
@group(0) @binding(1) var<storage, read_write> d: array<u32>;
@group(0) @binding(2) var<uniform> p: Params;
@compute @workgroup_size(64) fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let x = i32(id.x); let y = i32(id.y);
    if (x >= p.w) { return; }
    let i = y * p.w + x;
    let r = u32(round(oetf(s[3 * i]) * 255.0));
    let g = u32(round(oetf(s[3 * i + 1]) * 255.0));
    let b = u32(round(oetf(s[3 * i + 2]) * 255.0));
    d[i] = r | (g << 8u) | (b << 16u) | (255u << 24u);
}
