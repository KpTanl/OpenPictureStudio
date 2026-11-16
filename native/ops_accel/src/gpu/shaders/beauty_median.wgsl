// rows.x 非零时每一行各自求中位数：工作组 y 是行号，结果按行排成 3 列
struct Params { source: vec4<u32>, rows: vec4<u32> };
@group(0) @binding(0) var<storage,read> source: array<vec4<f32>>;
@group(0) @binding(1) var<storage,read_write> result: array<vec4<f32>>;
@group(0) @binding(2) var<uniform> p: Params;
var<workgroup> counts: array<vec2<u32>,256>;
var<workgroup> state: vec4<u32>;
var<workgroup> count: u32;
fn key(v:f32)->u32 {let b=bitcast<u32>(v);return select(b^0x80000000u,~b,(b&0x80000000u)!=0u);}
fn unkey(k:u32)->f32 {return bitcast<f32>(select(~k,k^0x80000000u,(k&0x80000000u)!=0u));}
fn sample(i:u32,row:u32)->vec4<f32> {return source[p.source.w+(row+i/p.source.x)*p.source.z+i%p.source.x];}
@compute @workgroup_size(256)
fn main(@builtin(local_invocation_index) lane:u32, @builtin(workgroup_id) group:vec3<u32>) {
    let size=select(p.source.x*p.source.y,p.source.x,p.rows.x!=0u);
    var valid=0u;
    for(var i=lane;i<size;i+=256u) {if sample(i,group.y).a>0.0 {valid++;}}
    counts[lane]=vec2<u32>(valid,0u);
    workgroupBarrier();
    for(var step=128u;step>0u;step/=2u) {
        if lane<step {counts[lane]+=counts[lane+step];}
        workgroupBarrier();
    }
    if lane==0u {count=counts[0].x;state=vec4<u32>(0u,(max(count,1u)-1u)/2u,0u,count/2u);}
    workgroupBarrier();
    for(var bit=32u;bit>0u;bit--) {
        let mask=1u<<(bit-1u);
        var zeros=vec2<u32>(0u);
        for(var i=lane;i<size;i+=256u) {
            let value=sample(i,group.y);
            if value.a>0.0 {
                let prefix=key(value[group.x])&(~(mask-1u));
                zeros+=vec2<u32>(select(0u,1u,prefix==state.x),select(0u,1u,prefix==state.z));
            }
        }
        counts[lane]=zeros;
        workgroupBarrier();
        for(var step=128u;step>0u;step/=2u) {
            if lane<step {counts[lane]+=counts[lane+step];}
            workgroupBarrier();
        }
        if lane==0u {
            if state.y>=counts[0].x {state.x|=mask;state.y-=counts[0].x;}
            if state.w>=counts[0].y {state.z|=mask;state.w-=counts[0].y;}
        }
        workgroupBarrier();
    }
    if lane==0u {
        var value=vec4<f32>(0.0);
        if count>0u {value=vec4<f32>((unkey(state.x)+unkey(state.z))*.5,0.0,0.0,f32(count));}
        result[group.y*3u+group.x]=value;
    }
}
