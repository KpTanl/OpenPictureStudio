struct Params { count:u32, row:u32, unused:vec2<u32> };
@group(0) @binding(0) var<storage,read_write> tensor:array<f32>;
@group(0) @binding(1) var<uniform> p:Params;
@compute @workgroup_size(64)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let index=id.y*p.row+id.x;if index<p.count {tensor[index]=0.0;}
}
