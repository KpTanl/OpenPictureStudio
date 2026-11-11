struct Params { source:vec4<u32>, roots:vec4<u32>, args:vec4<f32> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> roots:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p:Params;
var<workgroup> lows:array<vec2<u32>,256>;
var<workgroup> highs:array<vec2<u32>,256>;
var<workgroup> areas:array<u32,256>;
var<workgroup> sums:array<vec2<u32>,256>;
var<workgroup> orders:array<u32,256>;
fn even_round(v:f32)->f32 {let lo=floor(v);let t=v-lo;return lo+select(0.0,1.0,t>0.5||(t==0.5&&i32(lo)%2!=0));}
@compute @workgroup_size(256)
fn main(@builtin(local_invocation_index) lane:u32,@builtin(workgroup_id) group:vec3<u32>) {
    let root=roots[p.roots.w+(group.x/p.roots.x)*p.roots.z+group.x%p.roots.x].x;
    var low=p.source.xy;
    var high=vec2<u32>(0u);
    var area=0u;
    var total=vec2<u32>(0u);
    var order=0xffffffffu;
    for(var i=lane;i<p.source.x*p.source.y;i+=256u) {
        let pos=vec2<u32>(i%p.source.x,i/p.source.x);
        if source[p.source.w+pos.y*p.source.z+pos.x].x==root {
            low=min(low,pos);high=max(high,pos);area++;total+=pos;
            order=min(order,(pos.y/2u)*((p.source.x+1u)/2u)+pos.x/2u);
        }
    }
    lows[lane]=low;highs[lane]=high;areas[lane]=area;sums[lane]=total;orders[lane]=order;
    workgroupBarrier();
    for(var step=128u;step>0u;step/=2u) {
        if lane<step {
            lows[lane]=min(lows[lane],lows[lane+step]);highs[lane]=max(highs[lane],highs[lane+step]);
            areas[lane]+=areas[lane+step];sums[lane]+=sums[lane+step];orders[lane]=min(orders[lane],orders[lane+step]);
        }
        workgroupBarrier();
    }
    if lane==0u {
        let lo=lows[0];let hi=highs[0];let n=areas[0];let d=p.args.x;
        let diameter=2.0*sqrt(f32(n)/3.141592653589793);let pad=max(1.0,even_round(.03*d));
        let first=group.x*4u;
        result[first]=vec4<f32>(vec2<f32>(lo),vec2<f32>(hi-lo+1u));
        result[first+1u]=vec4<f32>(f32(n),vec2<f32>(sums[0])/max(1.0,f32(n)),root);
        result[first+2u]=vec4<f32>(max(vec2<f32>(0.0),vec2<f32>(lo)-pad),min(vec2<f32>(p.source.xy),vec2<f32>(hi)+1.0+pad));
        result[first+3u]=vec4<f32>(f32(orders[0]),diameter,select(0.0,1.0,diameter>=.01*d&&diameter<=.04*d),0.0);
    }
}
