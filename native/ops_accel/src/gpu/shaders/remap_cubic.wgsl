
struct Params { w: i32, h: i32, ow: i32, oh: i32, r: i32, eps: f32, bx: i32, by: i32, bw: i32, bh: i32, pad0: i32, pad1: i32 };

@group(0) @binding(0) var<storage, read> rgb: array<f32>;
@group(0) @binding(1) var<storage, read> field: array<f32>;
@group(0) @binding(2) var<storage, read_write> o: array<f32>;
@group(0) @binding(3) var<uniform> p: Params;
fn cubic(x: f32) -> vec4<f32> {
    let A = -0.75;
    let c0 = ((A * (x + 1.0) - 5.0 * A) * (x + 1.0) + 8.0 * A) * (x + 1.0) - 4.0 * A;
    let c1 = ((A + 2.0) * x - (A + 3.0)) * x * x + 1.0;
    let c2 = ((A + 2.0) * (1.0 - x) - (A + 3.0)) * (1.0 - x) * (1.0 - x) + 1.0;
    return vec4<f32>(c0, c1, c2, 1.0 - c0 - c1 - c2);
}
@compute @workgroup_size(64) fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if (i32(id.x) >= p.bw) { return; }
    let x = p.bx + i32(id.x); let y = p.by + i32(id.y);
    let W = p.w; let H = p.h;
    let index = y * W + x;
    let out = 3 * (i32(id.y) * p.bw + i32(id.x));
    let ox = field[2 * index]; let oy = field[2 * index + 1];
    if (ox == 0.0 && oy == 0.0) {
        o[out] = rgb[3 * index]; o[out + 1] = rgb[3 * index + 1]; o[out + 2] = rgb[3 * index + 2];
        return;
    }
    let mx = clamp(f32(x) + ox, 0.0, f32(W - 1)); let my = clamp(f32(y) + oy, 0.0, f32(H - 1));
    let sx = i32(round(mx * 32.0)); let sy = i32(round(my * 32.0));
    let ix = sx >> 5u; let iy = sy >> 5u;
    let cx = cubic(f32(sx & 31) / 32.0); let cy = cubic(f32(sy & 31) / 32.0);
    let fx = i32(floor(mx)); let fy = i32(floor(my));
    var total = vec3<f32>(0.0);
    var lo = vec3<f32>(1e30);
    var hi = vec3<f32>(-1e30);
    for (var j = 0; j < 4; j++) {
        for (var i = 0; i < 4; i++) {
            let px = clamp(ix + i - 1, 0, W - 1); let py = clamp(iy + j - 1, 0, H - 1);
            let k = 3 * (py * W + px);
            total += cx[i] * cy[j] * vec3<f32>(rgb[k], rgb[k + 1], rgb[k + 2]);
            let nx = clamp(fx + i - 1, 0, W - 1); let ny = clamp(fy + j - 1, 0, H - 1);
            let n = 3 * (ny * W + nx);
            let v = vec3<f32>(rgb[n], rgb[n + 1], rgb[n + 2]);
            lo = min(lo, v); hi = max(hi, v);
        }
    }
    let c = clamp(total, lo, hi);
    o[out] = c.x; o[out + 1] = c.y; o[out + 2] = c.z;
}
