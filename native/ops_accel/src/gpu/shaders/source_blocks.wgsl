@group(0) @binding(0) var<storage, read> packed: array<u32>;
@group(0) @binding(1) var<storage, read> blocks: array<vec4<u32>>;
@group(0) @binding(2) var<storage, read_write> output: array<atomic<u32>>;
@group(0) @binding(3) var<storage, read_write> status: array<u32>;
@group(0) @binding(4) var<uniform> batch: vec4<u32>;
var<workgroup> source_at: u32;
var<workgroup> target_at: u32;
var<workgroup> length: u32;
var<workgroup> token: u32;
var<workgroup> offset: u32;
var<workgroup> failed: u32;
fn byte_at(index: u32) -> u32 { return (packed[index / 4u] >> ((index % 4u) * 8u)) & 255u; }
fn result_at(index: u32) -> u32 { return (atomicLoad(&output[index / 4u]) >> ((index % 4u) * 8u)) & 255u; }
fn put(index: u32, value: u32) {
    let shift = (index % 4u) * 8u;
    atomicAnd(&output[index / 4u], ~(255u << shift));
    atomicOr(&output[index / 4u], (value & 255u) << shift);
}
fn extended(base: u32, end: u32, maximum: u32) -> u32 {
    var count = base;
    if base == 15u {
        loop {
            if source_at >= end { failed = 1u; break; }
            let value = byte_at(source_at);
            source_at++;
            count += value;
            if count > maximum { failed = 1u; break; }
            if value != 255u { break; }
        }
    }
    return count;
}
@compute @workgroup_size(128)
fn main(@builtin(workgroup_id) group: vec3<u32>, @builtin(local_invocation_index) lane: u32) {
    let number = batch.x + group.x;
    let block = blocks[number];
    let end = block.x + block.y;
    if lane == 0u { source_at = block.x; target_at = 0u; failed = 0u; }
    workgroupBarrier();
    loop {
        if lane == 0u {
            if source_at >= end { failed = select(1u, 2u, source_at == end && target_at == block.w); }
            else {
                token = byte_at(source_at);
                source_at++;
                length = extended(token >> 4u, end, block.w);
                if length > end - source_at || length > block.w - target_at { failed = 1u; }
            }
        }
        workgroupBarrier();
        if workgroupUniformLoad(&failed) != 0u { break; }
        for (var i = lane; i < length; i += 128u) { put(block.z + target_at + i, byte_at(source_at + i)); }
        storageBarrier();
        workgroupBarrier();
        if lane == 0u {
            source_at += length;
            target_at += length;
            if source_at == end { failed = select(1u, 2u, target_at == block.w); }
            else if end - source_at < 2u { failed = 1u; }
            else {
                offset = byte_at(source_at) | (byte_at(source_at + 1u) << 8u);
                source_at += 2u;
                length = extended(token & 15u, end, block.w) + 4u;
                if offset == 0u || offset > target_at || length > block.w - target_at { failed = 1u; }
            }
        }
        workgroupBarrier();
        if workgroupUniformLoad(&failed) != 0u { break; }
        for (var i = lane; i < length; i += 128u) {
            put(block.z + target_at + i, result_at(block.z + target_at - offset + i % offset));
        }
        storageBarrier();
        workgroupBarrier();
        if lane == 0u { target_at += length; }
        workgroupBarrier();
    }
    if lane == 0u { status[number] = select(1u, 0u, failed == 2u); }
}
