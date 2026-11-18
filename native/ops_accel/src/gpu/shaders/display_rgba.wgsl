struct Params { w: u32, h: u32, ow: u32, oh: u32, stride: u32, offset: u32, ox: u32, oy: u32, rw: u32, rh: u32, pad0: u32, pad1: u32 };
@group(0) @binding(0) var<storage, read> source: array<vec4<f32>>;
@group(0) @binding(1) var<storage, read_write> out_pixels: array<u32>;
@group(0) @binding(2) var<uniform> p: Params;

fn quantize(v0: vec4<f32>) -> vec4<f32> {
    let v = clamp(v0, vec4<f32>(0.0), vec4<f32>(1.0));
    let rgb = select(1.055 * pow(v.rgb, vec3<f32>(1.0 / 2.4)) - 0.055, 12.92 * v.rgb,
                     v.rgb <= vec3<f32>(0.0031308));
    let values = vec4<f32>(rgb, v.a) * 255.0;
    let low = floor(values);
    let fraction = values - low;
    return low + select(vec4<f32>(0.0), vec4<f32>(1.0),
                         (fraction > vec4<f32>(0.5)) | ((fraction == vec4<f32>(0.5)) & (vec4<u32>(low) % vec4<u32>(2u) != vec4<u32>(0u))));
}

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if (id.x >= p.rw || id.y >= p.rh) { return; }
    let at = id.xy + vec2<u32>(p.ox, p.oy);
    var value: vec4<f32>;
    if (p.w == p.ow && p.h == p.oh) {
        value = quantize(source[p.offset + at.y * p.stride + at.x]);
    } else {
        let scale = vec2<f32>(f32(p.w) / f32(p.ow), f32(p.h) / f32(p.oh));
        let lo = vec2<f32>(at) * scale;
        let hi = min(vec2<f32>(at + vec2<u32>(1u)) * scale, vec2<f32>(f32(p.w), f32(p.h)));
        var sum = vec4<f32>(0.0);
        var correction = vec4<f32>(0.0);
        for (var y = u32(floor(lo.y)); y < min(p.h, u32(ceil(hi.y))); y++) {
            let wy = max(0.0, min(hi.y, f32(y + 1u)) - max(lo.y, f32(y)));
            for (var x = u32(floor(lo.x)); x < min(p.w, u32(ceil(hi.x))); x++) {
                let weight = wy * max(0.0, min(hi.x, f32(x + 1u)) - max(lo.x, f32(x)));
                let pixel = quantize(source[p.offset + y * p.stride + x]);
                let coverage = pixel.a * weight;
                let contribution = vec4<f32>(pixel.rgb * coverage, coverage);
                let updated = sum + contribution;
                correction += select((contribution - updated) + sum, (sum - updated) + contribution, abs(sum) >= abs(contribution));
                sum = updated;
            }
        }
        sum += correction;
        value = vec4<f32>(0.0, 0.0, 0.0, sum.a / ((hi.x - lo.x) * (hi.y - lo.y)));
        if (sum.a > 0.0) { value = vec4<f32>(sum.rgb / sum.a, value.a); }
        let low = floor(value);
        let fraction = value - low;
        value = low + select(vec4<f32>(0.0), vec4<f32>(1.0),
                           (fraction > vec4<f32>(0.5)) | ((fraction == vec4<f32>(0.5)) & (vec4<u32>(low) % vec4<u32>(2u) != vec4<u32>(0u))));
    }
    let q = vec4<u32>(clamp(value, vec4<f32>(0.0), vec4<f32>(255.0)));
    out_pixels[id.y * p.rw + id.x] = q.r | (q.g << 8u) | (q.b << 16u) | (q.a << 24u);
}
