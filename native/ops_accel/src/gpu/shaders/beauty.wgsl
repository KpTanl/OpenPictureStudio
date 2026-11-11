struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,12> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(q:vec2<u32>,s:vec4<u32>)->u32 {return s.w+min(q.y,s.y-1u)*s.z+min(q.x,s.x-1u);}
fn lum(v:vec3<f32>)->f32 {return dot(v,vec3<f32>(.2126,.7152,.0722));}
fn smooth01(v:f32)->f32 {let t=clamp(v,0.0,1.0);return t*t*(3.0-2.0*t);}
fn lab(rgb:vec3<f32>)->vec3<f32> {
    let xyz=vec3<f32>(dot(rgb,vec3<f32>(.4124564,.3575761,.1804375)),dot(rgb,vec3<f32>(.2126729,.7151522,.0721750)),dot(rgb,vec3<f32>(.0193339,.1191920,.9503041)))/vec3<f32>(.95047,1.0,1.08883);
    let f=select(((24389.0/27.0)*xyz+16.0)/116.0,sign(xyz)*pow(abs(xyz),vec3<f32>(1.0/3.0)),xyz>vec3<f32>(216.0/24389.0));
    return vec3<f32>(116.0*f.y-16.0,500.0*(f.x-f.y),200.0*(f.y-f.z));
}
fn rgb(v:vec3<f32>)->vec3<f32> {
    let fy=(v.x+16.0)/116.0;let f=vec3<f32>(fy+v.y/500.0,fy,fy-v.z/200.0);
    let xyz=select((116.0*f-16.0)/(24389.0/27.0),f*f*f,f>vec3<f32>(6.0/29.0))*vec3<f32>(.95047,1.0,1.08883);
    return vec3<f32>(dot(xyz,vec3<f32>(3.240454912185669,-1.5371389389038086,-.4985315799713135)),dot(xyz,vec3<f32>(-.969266414642334,1.8760108947753906,.041556086391210556)),dot(xyz,vec3<f32>(.055643416941165924,-.20402584969997406,1.057225227355957)));
}
fn gamut(v:vec3<f32>)->vec3<f32> {
    let y=clamp(lum(v),0.0,1.0);let c=v-lum(v);var scale=1.0;
    for(var i=0u;i<3u;i++) {if c[i]>1e-8 {scale=min(scale,(1.0-y)/c[i]);}else if c[i]<-1e-8 {scale=min(scale,-y/c[i]);}}
    return y+c*clamp(scale,0.0,1.0);
}
fn skin(v:vec3<f32>)->f32 {return clamp((v.y+4.0)/12.0,0.0,1.0)*clamp((38.0-v.y)/12.0,0.0,1.0)*clamp((v.z+5.0)/12.0,0.0,1.0)*clamp((55.0-v.z)/15.0,0.0,1.0)*clamp((v.x-3.0)/12.0,0.0,1.0);}
fn reflect(v:i32,n:i32)->u32 {if n<=1 {return 0u;}let period=2*n-2;let k=((v%period)+period)%period;return u32(select(k,period-k,k>=n));}
fn load_ref(q:vec2<i32>)->vec4<f32> {return source[p.source.w+reflect(q.y,i32(p.source.y))*p.source.z+reflect(q.x,i32(p.source.x))];}
fn gradient(q:vec2<i32>,channel:u32)->f32 {
    let dx=(load_ref(q+vec2<i32>(1,-1))[channel]+2.0*load_ref(q+vec2<i32>(1,0))[channel]+load_ref(q+vec2<i32>(1,1))[channel]-load_ref(q+vec2<i32>(-1,-1))[channel]-2.0*load_ref(q+vec2<i32>(-1,0))[channel]-load_ref(q+vec2<i32>(-1,1))[channel])/8.0;
    let dy=(load_ref(q+vec2<i32>(-1,1))[channel]+2.0*load_ref(q+vec2<i32>(0,1))[channel]+load_ref(q+vec2<i32>(1,1))[channel]-load_ref(q+vec2<i32>(-1,-1))[channel]-2.0*load_ref(q+vec2<i32>(0,-1))[channel]-load_ref(q+vec2<i32>(1,-1))[channel])/8.0;
    return length(vec2<f32>(dx,dy));
}
fn landmark(index:u32)->vec2<f32> {return auxiliary[p.auxiliary.w+u32(p.args[0].x)*p.auxiliary.z+8u+index].xy*p.args[0].yz-p.args[1].xy;}
fn chain_height(offset:u32,u:f32,origin:vec2<f32>,axis:vec2<f32>,normal:vec2<f32>)->f32 {
    var lo=-3.4e38;var hi=3.4e38;var ly=0.0;var hy=0.0;var minx=3.4e38;var maxx=-3.4e38;var miny=0.0;var maxy=0.0;
    for(var i=0u;i<9u;i++) {let delta=landmark(u32(data[offset+i]))-origin;let x=dot(delta,axis);let y=dot(delta,normal);
        if x<minx {minx=x;miny=y;}if x>=maxx {maxx=x;maxy=y;}
        if x<=u&&x>=lo {lo=x;ly=y;}if x>u&&x<hi {hi=x;hy=y;}}
    if u<minx {return miny;}if u>=maxx {return maxy;}return mix(ly,hy,(u-lo)/max(hi-lo,1e-20));
}
fn bump(grid:vec2<f32>,center:vec2<f32>,radius:f32)->f32 {let delta=grid-center;return pow(max(0.0,1.0-dot(delta,delta)/max(radius*radius,1.0)),2.0);}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let q=id.xy;let mode=p.output.z;
    let s=source[at(q,p.source)];let a=auxiliary[at(q,p.auxiliary)];let f=fields[at(q,p.field)];let e=extra[at(q,p.extra)];var value=s;
    switch mode {
        case 0u:{value=vec4<f32>(lab(s.rgb),s.a);}
        case 1u:{value=vec4<f32>(rgb(s.rgb),s.a);}
        case 2u:{value=vec4<f32>(clamp(lum(s.rgb),0.0,1.0));}
        case 3u:{value=vec4<f32>(s[u32(p.args[0].x)]*p.args[0].y+p.args[0].z);}
        case 4u:{value=vec4<f32>(a.x*skin(s.rgb));}
        case 5u:{
            let radius=i32(p.args[0].x);let horizontal=p.args[0].y==0.0;value=vec4<f32>(0.0);
            for(var k=-radius;k<=radius;k++) {value+=load_ref(vec2<i32>(q)+select(vec2<i32>(0,k),vec2<i32>(k,0),horizontal))*data[u32(k+radius)];}
        }
        case 6u:{value=s*a;}
        case 7u:{value=s+a;}
        case 8u:{value=vec4<f32>(s.rgb+a.rgb*p.args[0].x,max(s.a,a.a));}
        case 9u:{value=vec4<f32>(0.0);}
        case 10u:{value=vec4<f32>(s.rgb,a.x);}
        case 11u:{
            let pos=vec2<i32>(q)-vec2<i32>(p.args[0].xy);if all(pos>=vec2<i32>(0))&&all(pos<vec2<i32>(p.auxiliary.xy)) {
                let overlay=auxiliary[at(vec2<u32>(pos),p.auxiliary)];value=select(overlay,s+overlay,p.args[0].z!=0.0);}
        }
        case 12u:{let delta=a.rgb/select(1.0,max(1.0,a.a),p.args[0].x!=0.0);if any(delta!=vec3<f32>(0.0)) {value=vec4<f32>(gamut(s.rgb+delta),s.a);}}
        case 13u:{
            let t=p.args[0].x;let weight=e.z;let y=clamp(lum(s.rgb),0.0,1.0);var delta=vec3<f32>(0.0);var dy=0.0;
            if t!=0.0 {
                dy-=.95*t*(.6+.4*t)*weight*(f.x-f.y)+.18*pow(max(0.0,(t-.5)*2.0),2.0)*weight*(y-f.x);
                var local=e.xy;if f.z>1e-6 {local/=f.z;}
                let changed=a.rgb+vec3<f32>(0.0,clamp(local-a.yz,vec2<f32>(-4.0),vec2<f32>(4.0))*.4*t*t*weight);
                if weight>0.0 {delta+=rgb(changed)-s.rgb;}
            }
            dy+=.25*p.args[0].y*weight*f.y*(1.0-clamp(f.y,0.0,1.0));delta+=dy;
            if p.args[0].z!=0.0&&weight>0.0 {delta+=rgb(a.rgb+vec3<f32>(0.0,3.0*p.args[0].z*weight,0.0))-s.rgb;}
            value=vec4<f32>(delta,weight);
        }
        case 14u:{let teeth=f.x*clamp((a.x-35.0)/30.0,0.0,1.0)*clamp((35.0-length(a.yz))/20.0,0.0,1.0);
            var delta=vec3<f32>(0.0);if teeth>0.0 {let changed=vec3<f32>(a.x+4.0*p.args[0].x*teeth,a.y,a.z*(1.0-.4*p.args[0].x*teeth));delta=rgb(changed)-s.rgb;}value=vec4<f32>(delta,teeth);}
        case 15u:{value=vec4<f32>(vec3<f32>(.25*p.args[0].x*f.x*(s.x-a.x)),f.x);}
        case 16u:{value=vec4<f32>(s[u32(p.args[0].x)],a[u32(p.args[0].y)],f[u32(p.args[0].z)],e[u32(p.args[0].w)]);}
        case 17u:{value=vec4<f32>(s.rgb*a.x,a.x);}
        case 18u:{
            let center=auxiliary[p.auxiliary.w].rgb;let residual=s.rgb-center;
            let inverse_covariance=mat3x3<f32>(auxiliary[p.auxiliary.w+1u].rgb,auxiliary[p.auxiliary.w+2u].rgb,auxiliary[p.auxiliary.w+3u].rgb);
            let spread=auxiliary[p.auxiliary.w+4u].x;let dark=smooth01((s.x-(center.x-max(12.0,2.5*spread)))/max(6.0,spread));
            value=vec4<f32>((1.0-smooth01((dot(residual*inverse_covariance,residual)-4.0)/12.0))*dark,dark,0.0,0.0);
        }
        case 19u:{value=vec4<f32>(clamp(s.x,0.0,1.0)*a.y);}
        case 20u:{
            let d=p.args[0].x;let upper=1.0-smooth01((f.y/max(d,1e-6)-.30)/.40);let blend=upper*(1.0-smooth01(e.x/max(1.0,.25*d)));
            let gradient_value=gradient(vec2<i32>(q),0u);let old=a.x*skin(s.rgb);
            if p.args[0].y==0.0 {value=vec4<f32>(old*(1.0-blend+blend*f.z),old*select(0.0,1.0,gradient_value<5.0),f.w,0.0);}
            else {value=vec4<f32>(old*(1.0-blend)+f.z*f.w*a.y*blend,a.y*a.x*select(0.0,1.0,gradient_value<5.0),f.w,0.0);}
        }
        case 21u:{
            let first=u32(p.args[0].w);let origin=landmark(first);let end=landmark(u32(p.args[1].z));let width=length(end-origin);let axis=(end-origin)/max(width,1e-6);
            var normal=vec2<f32>(-axis.y,axis.x);if dot(normal,landmark(152u)-origin)<0.0 {normal=-normal;}
            let delta=vec2<f32>(q)-origin;let u=dot(delta,axis);let v=dot(delta,normal);
            value=vec4<f32>(u,v,chain_height(0u,u,origin,axis,normal),chain_height(9u,u,origin,axis,normal));
        }
        case 22u:{let d=p.args[0].x;let width=p.args[0].y;let site=p.args[0].z*width/8.0;
            value=vec4<f32>(select(0.0,1.0,s.y>=s.w+.12*d&&s.y<=s.w+.25*d&&a.y>.6&&abs(s.x-site)<.08*d));}
        case 23u:{
            let d=p.args[0].x;let width=p.args[0].y;let t=p.args[0].z;var count=0u;var lo=0u;var hi=0u;
            for(var i=0u;i<9u;i++) {if fields[p.field.w+i].a>0.0 {if count==0u {lo=i;hi=i;}if f32(i)*width/8.0<=a.x {lo=i;}if f32(i)*width/8.0>=a.x&&hi==lo {hi=i;}count++;}}
            value=vec4<f32>(0.0);if count>=2u {
                hi=lo;for(var i=lo+1u;i<9u;i++) {if fields[p.field.w+i].a>0.0 {hi=i;break;}}
                let low=fields[p.field.w+lo];let high=fields[p.field.w+hi];let factor=clamp((a.x-f32(lo)*width/8.0)/max(1e-6,f32(hi-lo)*width/8.0),0.0,1.0);let target_value=mix(low.rgb,high.rgb,factor);
                let pos=clamp(a.x/max(width,1e-6)*8.0,0.0,8.0);let k=u32(pos);let confidence=mix(fields[p.field.w+k].a,fields[p.field.w+min(k+1u,8u)].a,fract(pos));
                let lateral=smooth01(min(a.x,width-a.x)/max(1.0,.15*width));let band=smooth01((a.y-a.z)/max(1.0,.10*d))*(1.0-smooth01((a.y-a.w-.05*d)/max(1.0,.05*d)))*lateral*e.a;
                let weight=band*confidence*smooth01(max(0.0,target_value.x-s.x)/4.0)*.60*t;
                let correction=vec3<f32>(max(0.0,target_value.x-s.x),(target_value.yz-s.yz)*.45);let current=lab(e.rgb);
                if weight>0.0 {value=vec4<f32>(rgb(current+correction*weight)-e.rgb,weight);}
            }
        }
        case 24u:{value=vec4<f32>(s.rgb+a.rgb,max(s.a,a.a));}
        case 25u:{value=vec4<f32>(s.rgb,f.x);}
        case 26u:{value=vec4<f32>(s.rgb,1.0);}
        case 27u:{value=vec4<f32>(max(s.x,a.x));}
        case 28u:{value=vec4<f32>(s.xy/max(1.0,a.x),0.0,0.0);}
        case 29u:{value=vec4<f32>(gradient(vec2<i32>(q),u32(p.args[0].x)));}
        case 30u:{
            let center=(landmark(0u)+landmark(1u))*.5;value=vec4<f32>(center,s.zw);
        }
        case 31u:{
            var centers:array<vec2<f32>,2>;for(var eye=0u;eye<2u;eye++) {var center=vec2<f32>(0.0);for(var i=0u;i<16u;i++) {center+=landmark(u32(data[eye*16u+i]));}centers[eye]=center/16.0;}
            let center=(centers[0]+centers[1])*.5;let distance=length(centers[1]-centers[0]);let axis=(centers[1]-centers[0])/max(distance,1e-6);
            var normal=vec2<f32>(-axis.y,axis.x);if dot(normal,landmark(152u)-center)<0.0 {normal=-normal;}
            let delta=vec2<f32>(q)-center;value=vec4<f32>(dot(delta,axis),dot(delta,normal),0.0,0.0);
        }
        case 32u:{let d=p.args[0].x;let grad=gradient(vec2<i32>(q),0u);let seed=a.x>.9&&f.y>.35*d&&f.y<1.1*d&&abs(f.x)>.28*d&&abs(f.x)<.8*d&&grad<5.0;
            let forehead=p.args[0].y!=0.0&&a.x>.9&&f.y<-.35*d&&e.x>.8&&grad<5.0;value=vec4<f32>(select(0.0,1.0,seed||forehead));}
        case 34u:{
            let d=p.args[0].w;let grid=(vec2<f32>(q)+.5)*p.args[1].zw/vec2<f32>(p.output.xy)-.5;var displacement=vec2<f32>(0.0);
            var centers:array<vec2<f32>,2>;for(var eye=0u;eye<2u;eye++) {var center=vec2<f32>(0.0);for(var i=0u;i<16u;i++) {center+=landmark(u32(data[14u+eye*16u+i]));}centers[eye]=center/16.0;}
            let direction=(centers[1]-centers[0])/max(d,1e-6);let settings=p.args[2];
            if settings.x!=0.0 {for(var side=0u;side<2u;side++) {var anchor=vec2<f32>(0.0);for(var i=0u;i<7u;i++) {anchor+=landmark(u32(data[side*7u+i]));}anchor/=7.0;
                let vector=anchor-landmark(1u);displacement+=bump(grid,anchor,d*.65)*vector/max(length(vector),1e-6)*(.04*d*settings.x);}}
            for(var eye=0u;eye<2u;eye++) {let center=centers[eye];if settings.y!=0.0 {let factor=1.0+.08*settings.y;displacement+=(grid-center)*(1.0/factor-1.0)*bump(grid,center,d*.35);}
                if settings.z!=0.0 {displacement+=bump(grid,center,d*.4)*direction*(select(1.0,-1.0,eye==1u)*.025*d*settings.z);}}
            if settings.w!=0.0 {let center=(landmark(48u)+landmark(278u))*.5;for(var side=0u;side<2u;side++) {let point=landmark(select(48u,278u,side==1u));let vector=point-center;
                displacement+=bump(grid,point,d*.22)*vector/max(length(vector),1e-6)*(.025*d*settings.w);}}
            value=vec4<f32>(displacement,0.0,0.0);
        }
        case 35u:{value=vec4<f32>(s.rgb/max(1.0,s.a),min(1.0,s.a));}
        case 36u:{value=vec4<f32>(length(landmark(u32(p.args[1].z))-landmark(u32(p.args[0].w))),0.0,0.0,0.0);}
        case 37u:{value=vec4<f32>(a.rgb,max(a.a,f.a));if any(f.rgb!=vec3<f32>(0.0)) {value=vec4<f32>(e.rgb+f.rgb-s.rgb,value.a);}}
        case 38u:{
            let first=(q.y*p.output.x+q.x)*1024u;let last=min(first+1024u,p.source.x*p.source.y);var low=vec2<f32>(3.4e38);var high=vec2<f32>(-3.4e38);
            for(var i=first;i<last;i++) {let pos=vec2<u32>(i%p.source.x,i/p.source.x);let delta=source[at(pos,p.source)].xy;
                let uv=clamp(vec2<f32>(pos)+p.args[0].xy+delta,vec2<f32>(0.0),p.args[0].zw-1.0);
                let lo=floor(uv);low=min(low,lo-2.0);high=max(high,lo+4.0);}
            value=vec4<f32>(low,high);
        }
        case 39u:{var low=vec2<f32>(3.4e38);var high=vec2<f32>(-3.4e38);
            for(var i=0u;i<p.source.x*p.source.y;i++) {let v=source[at(vec2<u32>(i%p.source.x,i/p.source.x),p.source)];low=min(low,v.xy);high=max(high,v.zw);}
            value=vec4<f32>(max(low,vec2<f32>(0.0)),min(high,p.args[0].xy));}
        case 40u:{value=vec4<f32>(clamp(s.x/100.0,0.0,1.0));}
        default:{value=s+a*p.args[0].x+f*p.args[0].y+e*p.args[0].z;}
    }
    result[q.y*p.output.x+q.x]=value;
}
