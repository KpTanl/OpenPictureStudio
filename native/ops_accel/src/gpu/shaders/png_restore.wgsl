struct Params { image: vec4<u32>, wave: vec4<u32> };
@group(0) @binding(0) var<storage, read_write> bytes: array<atomic<u32>>;
@group(0) @binding(1) var<uniform> p: Params;
@group(0) @binding(2) var<storage, read_write> status: atomic<u32>;
var<workgroup> tile: array<atomic<u32>, 2210>;
var<workgroup> filters: array<u32, 16>;
fn get_byte(index: u32) -> u32 { return (atomicLoad(&bytes[index / 4u]) >> ((index % 4u) * 8u)) & 255u; }
fn put_byte(index: u32, value: u32) {
    let shift = (index % 4u) * 8u;
    atomicAnd(&bytes[index / 4u], ~(255u << shift));
    atomicOr(&bytes[index / 4u], (value & 255u) << shift);
}
fn local_get(index: u32) -> u32 { return (atomicLoad(&tile[index / 4u]) >> ((index % 4u) * 8u)) & 255u; }
fn local_put(index: u32, value: u32) {
    let shift = (index % 4u) * 8u;
    atomicAnd(&tile[index / 4u], ~(255u << shift));
    atomicOr(&tile[index / 4u], (value & 255u) << shift);
}
@compute @workgroup_size(128)
fn main(@builtin(workgroup_id) group: vec3<u32>, @builtin(local_invocation_index) lane: u32) {
    let tx = p.wave.y + group.x;
    let ox = tx * 64u;
    let oy = (p.wave.x - tx) * 16u;
    let cols = min(64u, p.image.x - ox);
    let rows = min(16u, p.image.y - oy);
    let step = p.image.z;
    let pitch = (cols + 1u) * step;
    let scan = p.image.x * step + 1u;
    let count = (rows + 1u) * pitch;
    for (var i = lane; i < 2210u; i += 128u) { atomicStore(&tile[i], 0u); }
    workgroupBarrier();
    for (var i = lane; i < count; i += 128u) {
        let row = i32(oy) + i32(i / pitch) - 1;
        let offset = (i32(ox) - 1) * i32(step) + i32(i % pitch);
        var value = 0u;
        if row >= 0 && offset >= 0 { value = get_byte(u32(row) * scan + u32(offset) + 1u); }
        local_put(i, value);
    }
    if lane < rows {
        filters[lane] = get_byte((oy + lane) * scan);
        if filters[lane] > 4u { atomicStore(&status, 1u); }
    }
    workgroupBarrier();
    let dy = lane / step;
    let channel = lane % step;
    for (var diagonal = 0u; diagonal < rows + cols - 1u; diagonal++) {
        let dx = i32(diagonal) - i32(dy);
        if dy < rows && dx >= 0 && dx < i32(cols) {
            let i = (dy + 1u) * pitch + (u32(dx) + 1u) * step + channel;
            let a = local_get(i - step);
            let b = local_get(i - pitch);
            let c = local_get(i - pitch - step);
            var predictor = 0u;
            switch filters[dy] {
                case 1u: { predictor = a; }
                case 2u: { predictor = b; }
                case 3u: { predictor = (a + b) / 2u; }
                case 4u: {
                    let da = abs(i32(b) - i32(c));
                    let db = abs(i32(a) - i32(c));
                    let dc = abs(i32(a) + i32(b) - 2 * i32(c));
                    predictor = select(select(c, b, db <= dc), a, da <= db && da <= dc);
                }
                default: {}
            }
            local_put(i, local_get(i) + predictor);
        }
        workgroupBarrier();
    }
    for (var i = lane; i < rows * cols * step; i += 128u) {
        let row = i / (cols * step);
        let offset = i % (cols * step);
        put_byte((oy + row) * scan + ox * step + offset + 1u, local_get((row + 1u) * pitch + step + offset));
    }
}
