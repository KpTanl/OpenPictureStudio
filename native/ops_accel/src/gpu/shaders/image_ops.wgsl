struct Params { a: vec4<u32>, b: vec4<u32>, o: vec4<u32>, v0: vec4<f32>, v1: vec4<f32>, v2: vec4<f32>, v3: vec4<f32>, v4: vec4<f32> };
@group(0) @binding(0) var<storage, read> source: array<vec4<f32>>;
@group(0) @binding(1) var<storage, read> auxiliary: array<vec4<f32>>;
@group(0) @binding(2) var<storage, read_write> result: array<vec4<f32>>;
@group(0) @binding(3) var<uniform> p: Params;
fn load(x: i32, y: i32) -> vec4<f32> {
    return source[p.a.w + u32(clamp(y, 0, i32(p.a.y)-1)) * p.a.z + u32(clamp(x, 0, i32(p.a.x)-1))];
}
fn premul(v: vec4<f32>) -> vec4<f32> { return vec4<f32>(v.rgb * v.a, v.a); }
fn straight(v: vec4<f32>, epsilon: f32) -> vec4<f32> {
    let alpha = clamp(v.a, 0.0, 1.0);
    var rgb = vec3<f32>(0.0);
    if alpha > epsilon { rgb = v.rgb / alpha; }
    return vec4<f32>(rgb, alpha);
}
fn cubic(a: vec4<f32>, b: vec4<f32>, c: vec4<f32>, d: vec4<f32>, t: f32) -> vec4<f32> {
    return b + t * ((-a+c) + t * (2.0*(a-b)+c-d + t * (-a+b-c+d)));
}
fn wide_add(a:vec2<u32>,b:vec2<u32>)->vec2<u32> {
    let lo=a.x+b.x; return vec2<u32>(lo,a.y+b.y+select(0u,1u,lo<a.x));
}
fn wide_neg(a:vec2<u32>)->vec2<u32> { return wide_add(~a,vec2<u32>(1u,0u)); }
fn wide_mul(a:vec2<u32>,b:u32)->vec2<u32> {
    let p0=(a.x&65535u)*b; let p1=(a.x>>16u)*b+(p0>>16u);
    return vec2<u32>((p1<<16u)|(p0&65535u),a.y*b+(p1>>16u));
}
fn coefficient(v:vec4<f32>)->vec2<u32> { return vec2<u32>(u32(v.x)|(u32(v.y)<<16u),u32(v.z)|(u32(v.w)<<16u)); }
fn affine(x:u32,y:u32)->vec4<f32> {
    let a=coefficient(p.v0); let b=coefficient(p.v1); let c=coefficient(p.v2); let f=coefficient(p.v3);
    let u=wide_add(wide_add(wide_mul(a,2u*x+1u),wide_mul(b,2u*y+1u)),wide_mul(c,2u));
    let v=wide_add(wide_add(wide_mul(wide_neg(b),2u*x+1u),wide_mul(a,2u*y+1u)),wide_mul(f,2u));
    if i32(u.y)<0||i32(v.y)<0||i32(u.y)>>9>=i32(p.a.x)||i32(v.y)>>9>=i32(p.a.y) { return vec4<f32>(0.0); }
    let us=wide_add(u,vec2<u32>(0u,0xffffff00u)); let vs=wide_add(v,vec2<u32>(0u,0xffffff00u));
    let base=vec2<i32>(i32(us.y)>>9,i32(vs.y)>>9);
    let t=vec2<f32>(f32(us.y&511u)/512.0+f32(us.x)/2199023255552.0,f32(vs.y&511u)/512.0+f32(vs.x)/2199023255552.0);
    var rows:array<vec4<f32>,4>;
    for(var j=0;j<4;j++) {
        let row=base.y+j-1;
        if j>0&&(row<0||row>=i32(p.a.y)) { rows[j]=rows[j-1]; }
        else { rows[j]=cubic(premul(load(base.x-1,row)),premul(load(base.x,row)),premul(load(base.x+1,row)),premul(load(base.x+2,row)),t.x); }
    }
    return straight(cubic(rows[0],rows[1],rows[2],rows[3],t.y),1e-8);
}
fn weights(t: f32) -> vec4<f32> {
    let a = -0.75; let u=t+1.0; let v=1.0-t;
    let x=((a*u-5.0*a)*u+8.0*a)*u-4.0*a;
    let y=((a+2.0)*t-(a+3.0))*t*t+1.0;
    let z=((a+2.0)*v-(a+3.0))*v*v+1.0;
    return vec4<f32>(x,y,z,1.0-x-y-z);
}
fn even_round(x: vec2<f32>) -> vec2<f32> {
    let lo=floor(x); let f=x-lo;
    return lo+select(vec2<f32>(0.0),vec2<f32>(1.0),(f>vec2<f32>(0.5))|((f==vec2<f32>(0.5))&(vec2<u32>(lo)%2u!=vec2<u32>(0u))));
}
fn remap(x: i32, y: i32) -> vec4<f32> {
    let local=vec2<u32>(vec2<i32>(x,y)-select(vec2<i32>(0),vec2<i32>(p.v0.xy),p.o.z==14u||p.o.z==15u));
    let field=auxiliary[p.b.w+local.y*p.b.z+local.x].xy;
    let origin=select(vec2<i32>(0),vec2<i32>(p.v0.zw),p.o.z==15u);
    if all(field==vec2<f32>(0.0)) { return load(x-origin.x,y-origin.y); }
    let limit=select(vec2<f32>(p.a.xy),p.v1.xy,p.o.z==15u);
    let uv=clamp(vec2<f32>(f32(x),f32(y))+field,vec2<f32>(0.0),limit-1.0);
    let scaled=vec2<i32>(even_round(uv*32.0)); let pos=scaled/32-origin;
    let wx=weights(f32(scaled.x&31)/32.0); let wy=weights(f32(scaled.y&31)/32.0);
    let near=vec2<i32>(floor(uv))-origin; var sum=vec4<f32>(0.0);
    var lo=vec3<f32>(3.4e38); var hi=vec3<f32>(-3.4e38); var amin=1.0; var amax=0.0;
    for(var j=0;j<4;j++) { for(var i=0;i<4;i++) {
        let pixel=load(pos.x+i-1,pos.y+j-1); let weight=wx[i]*wy[j];
        sum+=premul(pixel)*weight;
        let n=load(near.x+i-1,near.y+j-1); amin=min(amin,n.a); amax=max(amax,n.a);
        if n.a>1e-6 { lo=min(lo,n.rgb); hi=max(hi,n.rgb); }
    }}
    sum.a=clamp(sum.a,amin,amax);
    if lo.x==3.4e38 { lo=vec3<f32>(0.0); hi=vec3<f32>(0.0); }
    if sum.a>1e-6 { return vec4<f32>(clamp(sum.rgb,lo*sum.a,hi*sum.a)/sum.a,sum.a); }
    return vec4<f32>(0.0,0.0,0.0,sum.a);
}
fn lanczos(t0: f32) -> f32 {
    let t=abs(t0); if t==0.0 { return 1.0; } if t>=3.0 { return 0.0; }
    let x=t*3.141592653589793; return sin(x)*sin(x/3.0)/(x*x/3.0);
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if any(id.xy>=p.o.xy) { return; }
    let x=i32(id.x); let y=i32(id.y); let mode=p.o.z;
    var value=load(x,y);
    switch mode {
        case 1u: { value=affine(id.x,id.y); }
        case 2u: { value=premul(value); }
        case 3u: { value=straight(value,1e-8); }
        case 4u: { value.a*=auxiliary[p.b.w+id.y*p.b.z+id.x].x/255.0; }
        case 5u: { let q=vec2<i32>(x,y)-vec2<i32>(p.v0.xy); if all(q>=vec2<i32>(0))&&all(q<vec2<i32>(p.b.xy)) {
            let overlay=auxiliary[p.b.w+u32(q.y)*p.b.z+u32(q.x)];
            value=vec4<f32>(overlay.rgb,select(overlay.a,value.a,p.v0.z>0.0)); } }
        case 6u: { value=remap(x,y); }
        case 14u, 15u: { value=remap(x+i32(p.v0.x),y+i32(p.v0.y)); }
        case 7u: {
            let uv=clamp((vec2<f32>(id.xy)+0.5)*vec2<f32>(p.a.xy)/vec2<f32>(p.o.xy)-0.5,vec2<f32>(0.0),vec2<f32>(p.a.xy)-1.0);
            let q=vec2<i32>(floor(uv)); let t=fract(uv);
            value=mix(mix(load(q.x,q.y),load(q.x+1,q.y),t.x),mix(load(q.x,q.y+1),load(q.x+1,q.y+1),t.x),t.y);
        }
        case 8u, 9u: {
            let horizontal=mode==8u;
            let coordinate=select(y,x,horizontal); let limit=select(p.a.y,p.a.x,horizontal);
            let start=p.v0.x; let end=p.v0.y; let count=select(p.o.y,p.o.x,horizontal);
            let scale=(end-start)/f32(count); let filter_scale=max(scale,1.0); let center=start+(f32(coordinate)+0.5)*scale;
            let first=max(0,i32(center-3.0*filter_scale+0.5)); let last=min(i32(limit),i32(center+3.0*filter_scale+0.5));
            var total=vec4<f32>(0.0); var weight=0.0;
            for(var n=first;n<last;n++) { let w=lanczos((f32(n)-center+0.5)/filter_scale); weight+=w;
                total+=load(select(x,n,horizontal),select(n,y,horizontal))*w; }
            value=total/weight;
        }
        case 10u: {
            let index=id.y*p.o.x+id.x; let size=p.o.x*p.o.y;
            value=vec4<f32>(0.0);
            for(var c=0u;c<4u;c++) { let k=index*4u+c; let ch=k/size; let at=k%size; value[c]=source[p.a.w+(at/p.a.x)*p.a.z+at%p.a.x][ch]; }
        }
        case 11u: {
            let index=id.y*p.o.x+id.x; let size=p.o.x*p.o.y;
            for(var c=0u;c<4u;c++) { let k=c*size+index; value[c]=source[k/4u][k%4u]; }
        }
        case 12u: {
            let q=vec2<f32>(id.xy); var inside=all(q>=p.v0.xy)&&all(q<=p.v0.zw)&&all(q>=p.v1.xy)&&all(q<=p.v1.zw);
            if inside && p.v2.x>0.0 {
                let row=auxiliary[p.b.w+(id.y-u32(p.v0.y))*p.b.z];
                inside=q.x>=p.v0.x+row.x&&q.x<=p.v0.x+row.y;
            }
            value=vec4<f32>(max(value.x,select(0.0,255.0,inside)));
        }
        case 13u: { value=vec4<f32>(255.0-value.x); }
        default: {}
    }
    result[id.y*p.o.x+id.x]=value;
}
