struct Params { w:u32,h:u32,count:u32,subtract:u32 };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> edges:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p:Params;
fn intersection(e:vec4<f32>,y:f32)->f32 { return (y-e.y)*((e.z-e.x)/(e.w-e.y))+e.x; }
fn away(x:f32)->f32 { return sign(x)*floor(abs(x)+0.5); }
@compute @workgroup_size(8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    let row=id.x; if row>=p.h { return; } let y=f32(row);
    var hits:array<f32,1024>; var count=0u; var ymax=0.0;
    for(var n=0u;n<p.count;n++) { ymax=max(ymax,max(edges[n].y,edges[n].w)); }
    for(var n=0u;n<p.count;n++) {
        let e=edges[n]; let lo=min(e.y,e.w); let hi=max(e.y,e.w);
        if e.y==e.w || y<lo || y>hi { continue; }
        var x=intersection(e,y);
        if y==hi&&y<ymax { hits[count]=x; count++; }
        else if (y==lo||y==hi)&&e.x!=e.z {
            for(var k=0u;k<n;k++) {
                let other=edges[k]; if other.y==other.w || (y!=min(other.y,other.w)&&y!=max(other.y,other.w)) || other.x==other.z { continue; }
                if away(x)!=away(intersection(other,y)) { continue; }
                let offset=select(1.0,-1.0,y==hi); let next=y+offset;
                if next<min(other.y,other.w)||next>max(other.y,other.w) { continue; }
                let a=intersection(e,next); let b=intersection(other,next);
                if x>a+1.0&&x>b+1.0 { x=away(max(a,b))+1.0; }
                else if x<a-1.0&&x<b-1.0 { x=away(min(a,b))-1.0; }
                break;
            }
        }
        hits[count]=x; count++;
    }
    for(var n=1u;n<count;n++) { let value=hits[n]; var at=n;
        loop { if at==0u { break; } if hits[at-1u]<=value { break; } hits[at]=hits[at-1u]; at--; } hits[at]=value;
    }
    for(var x=0u;x<p.w;x++) { result[row*p.w+x]=source[row*p.w+x]; }
    let ink=vec4<f32>(select(255.0,0.0,p.subtract!=0u));
    for(var n=1u;n<count;n+=2u) {
        let first=max(0,i32(floor(hits[n-1u]+0.5))); let last=min(i32(p.w)-1,i32(ceil(hits[n]-0.5)));
        for(var x=first;x<=last;x++) { result[row*p.w+u32(x)]=ink; }
    }
    for(var n=0u;n<p.count;n++) { let e=edges[n]; if e.y!=y||e.w!=y { continue; }
        let first=max(0,i32(min(e.x,e.z))); let last=min(i32(p.w)-1,i32(max(e.x,e.z)));
        for(var x=first;x<=last;x++) { result[row*p.w+u32(x)]=ink; }
    }
}
