
struct Params { w: i32, h: i32, ow: i32, oh: i32, r: i32, eps: f32, bx: i32, by: i32, bw: i32, bh: i32, pad0: i32, pad1: i32 };

fn add(state: vec2<f32>, v: f32) -> vec2<f32> {
    let t = state.x + v;
    var c = state.y;
    if (abs(state.x) >= abs(v)) { c += (state.x - t) + v; } else { c += (v - t) + state.x; }
    return vec2<f32>(t, c);
}

@group(0) @binding(0) var<storage, read> s: array<f32>;
@group(0) @binding(1) var<storage, read_write> d: array<f32>;
@group(0) @binding(2) var<uniform> p: Params;

@compute @workgroup_size(16) fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let y = i32(id.x);
    if (y >= p.h) { return; }
    let w = p.w; let r = p.r; let base = y * w;
    let period = max(64, 2 * r + 1); var state = vec2<f32>(0.0);
    let end = min(r + 1, w);
    for (var i = 0; i < end; i++) { state = add(state, s[base + i]); }
    d[base] = (state.x + state.y) / f32(end);

    for (var x = 1; x < w; x++) {
        if (x % period == 0) {
            state = vec2<f32>(0.0);
            for (var i = max(x - r, 0); i < min(x + r + 1, w); i++) { state = add(state, s[base + i]); }
        } else {
            if (x + r < w) { state = add(state, s[base + x + r]); }
            if (x - r - 1 >= 0) { state = add(state, -s[base + x - r - 1]); }
        }
        d[base + x] = (state.x + state.y) / f32(min(x + r + 1, w) - max(x - r, 0));
    }
}
