struct Params { image: vec4<u32>, output: vec4<u32>, options: vec4<u32>, m0: vec4<f32>, m1: vec4<f32>, m2: vec4<f32> };
@group(0) @binding(0) var<storage, read> bytes: array<u32>;
@group(0) @binding(1) var<storage, read> lut: array<f32>;
@group(0) @binding(2) var<storage, read_write> result: array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p: Params;
fn get_byte(index: u32) -> u32 { return (bytes[index / 4u] >> ((index % 4u) * 8u)) & 255u; }
fn sample(index: u32) -> u32 {
    let first = get_byte(index);
    if p.image.w == 1u { return first; }
    let second = get_byte(index + 1u);
    return select(first | (second << 8u), (first << 8u) | second, p.output.w != 0u);
}
fn pixel(x: u32, y: u32) -> vec4<f32> {
    let step = p.image.z * p.image.w;
    let index = y * (p.image.x * step + p.output.z) + p.output.z + x * step;
    let first = sample(index);
    var rgb = vec3<f32>(lut[first]);
    if p.options.y != 0u {
        let scale = 65535.0;
        let xyz = vec3<f32>(f32(first), f32(sample(index + 2u)), f32(sample(index + 4u))) / scale;
        rgb = vec3<f32>(dot(xyz, p.m0.xyz), dot(xyz, p.m1.xyz), dot(xyz, p.m2.xyz));
    } else if p.image.z >= 3u {
        rgb = vec3<f32>(lut[first], lut[sample(index + p.image.w)], lut[sample(index + 2u * p.image.w)]);
    }
    var alpha = 1.0;
    if p.image.z == 2u || p.image.z == 4u {
        alpha = f32(sample(index + (p.image.z - 1u) * p.image.w)) / select(255.0, 65535.0, p.image.w == 2u);
    }
    return vec4<f32>(select(rgb, rgb * alpha, p.options.x != 0u), alpha);
}
fn lanczos(v: f32) -> f32 {
    let t = abs(v);
    if t == 0.0 { return 1.0; }
    if t >= 3.0 { return 0.0; }
    let x = t * 3.141592653589793;
    return sin(x) * sin(x / 3.0) / (x * x / 3.0);
}
@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if id.x >= p.output.x || id.y >= p.output.y { return; }
    let row = id.y + p.options.z;
    var value = pixel(id.x, row);
    if p.output.x != p.image.x {
        let scale = f32(p.image.x) / f32(p.output.x);
        let center = (f32(id.x) + 0.5) * scale;
        let support = max(1.0, scale);
        let first = max(0, i32(center - 3.0 * support + 0.5));
        let last = min(i32(p.image.x), i32(center + 3.0 * support + 0.5));
        var sum = vec4<f32>(0.0);
        var mass = 0.0;
        for (var x = first; x < last; x++) {
            let weight = lanczos((f32(x) - center + 0.5) / support);
            mass += weight;
            sum += pixel(u32(x), row) * weight;
        }
        value = sum / mass;
    }
    result[id.y * p.output.x + id.x] = value;
}
