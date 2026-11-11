
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
    if (x >= p.ow) { return; }
    let sx = f32(p.w) / f32(p.ow); let sy = f32(p.h) / f32(p.oh);
    let x0 = f32(x) * sx; let x1 = f32(x + 1) * sx;
    let y0 = f32(y) * sy; let y1 = f32(y + 1) * sy;
    var c = vec3<f32>(0.0);
    for (var yy = i32(floor(y0)); yy < min(i32(ceil(y1)), p.h); yy++) {
        let wy = min(f32(yy + 1), y1) - max(f32(yy), y0);
        for (var xx = i32(floor(x0)); xx < min(i32(ceil(x1)), p.w); xx++) {
            let wx = min(f32(xx + 1), x1) - max(f32(xx), x0);
            let i = 3 * (yy * p.w + xx);
            c += wx * wy * vec3<f32>(oetf(s[i]), oetf(s[i + 1]), oetf(s[i + 2]));
        }
    }
    let v = round(c * 255.0 / (sx * sy));
    d[y * p.ow + x] = u32(v.x) | (u32(v.y) << 8u) | (u32(v.z) << 16u) | (255u << 24u);
}
