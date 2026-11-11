struct Params { source:vec4<u32>, state:vec4<u32>, args:vec4<u32> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> states:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p:Params;
var<workgroup> counts:array<vec2<u32>,64>;
fn key(v:f32)->u32 {let b=bitcast<u32>(v);return select(b^0x80000000u,~b,(b&0x80000000u)!=0u);}
@compute @workgroup_size(64)
fn main(@builtin(local_invocation_index) lane:u32,@builtin(workgroup_id) group:vec3<u32>) {
    let size=p.source.x*p.source.y;
    let state=states[p.state.w+group.y*p.state.z];
    let mask=1u<<p.args.x;
    var zeros=vec2<u32>(0u);
    let first=group.x*1024u;
    for(var i=first+lane;i<min(first+1024u,size);i+=64u) {
        let value=source[p.source.w+(i/p.source.x)*p.source.z+i%p.source.x];
        if value.a>0.0 {
            let prefix=key(value[group.y])&(~(mask-1u));
            zeros+=vec2<u32>(select(0u,1u,prefix==bitcast<u32>(state.x)),select(0u,1u,prefix==bitcast<u32>(state.z)));
        }
    }
    counts[lane]=zeros;
    workgroupBarrier();
    for(var step=32u;step>0u;step/=2u) {
        if lane<step {counts[lane]+=counts[lane+step];}
        workgroupBarrier();
    }
    if lane==0u {result[group.y*((size+1023u)/1024u)+group.x]=vec4<f32>(vec2<f32>(counts[0]),0.0,0.0);}
}
