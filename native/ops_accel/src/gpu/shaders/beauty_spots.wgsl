struct Params { output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>, args:array<vec4<f32>,12> };
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(q:vec2<u32>,s:vec4<u32>)->u32 {return s.w+q.y*s.z+q.x;}
fn src(q:vec2<u32>)->vec4<f32> {return source[at(q,p.source)];}
fn aux(q:vec2<u32>)->vec4<f32> {return auxiliary[at(q,p.auxiliary)];}
fn smooth01(v:f32)->f32 {let t=clamp(v,0.0,1.0);return t*t*(3.0-2.0*t);}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) {return;}let q=id.xy;let k=q.y*p.output.x+q.x;let mode=p.output.z;var value=vec4<f32>(0.0);
    switch mode {
        case 0u:{value.x=select(0.0,f32(k+1u),src(q).x>0.0);}
        case 1u:{let label=src(q).x;if label>0.0 {var best=label;
            for(var y=-1;y<=1;y++) {for(var x=-1;x<=1;x++) {let pos=vec2<i32>(q)+vec2<i32>(x,y);if all(pos>=vec2<i32>(0))&&all(pos<vec2<i32>(p.source.xy)) {let other=src(vec2<u32>(pos)).x;if other>0.0 {best=min(best,other);}}}}
            let parent=u32(best)-1u;value.x=min(best,src(vec2<u32>(parent%p.source.x,parent/p.source.x)).x);}}
        case 2u:{value.x=select(0.0,1.0,src(q).x!=aux(q).x);}
        case 3u:{let s=src(q).x;value=vec4<f32>(s,0.0,0.0,select(0.0,1.0,s==f32(k+1u)));}
        case 5u:{for(var row=0u;row<p.source.y;row++) {let order=src(vec2<u32>(3u,row)).x;var rank=0u;
            for(var other=0u;other<p.source.y;other++) {let second=src(vec2<u32>(3u,other)).x;if second<order||(second==order&&other<row) {rank++;}}
            if rank==q.y {value=src(vec2<u32>(q.x,row));break;}}}
        case 6u:{let pos=q+vec2<u32>(p.args[0].xy);value=vec4<f32>(select(0.0,1.0,src(pos).x==p.args[0].z));}
        case 7u:{
            var total=vec4<f32>(0.0);var gradient=0.0;var count=0.0;
            for(var y=0u;y<p.field.y;y++) {for(var x=0u;x<p.field.x;x++) {if fields[at(vec2<u32>(x,y),p.field)].x>0.0 {let pos=vec2<u32>(x,y)+vec2<u32>(p.args[0].xy);total+=src(pos);gradient+=aux(pos).x;count+=1.0;}}}
            value=select(vec4<f32>(gradient/max(count,1.0),count,0.0,0.0),total/max(count,1.0),q.x==0u);
        }
        case 8u:{
            let box=p.args[0];let extent=vec2<i32>(box.zw-box.xy);let position=q+vec2<u32>(p.args[1].xy);let cx=i32(position.x);let cy=i32(position.y);let d=p.args[1].z;let center=extra[p.extra.w+2u].yz;let r=length(vec2<f32>(position)-center);
            let target_value=extra[p.extra.w];let target_g=extra[p.extra.w+1u].x;let count=extra[p.extra.w+1u].y;
            var valid=r>=.05*d&&r<=.20*d&&count>=8.0&&cx>=extent.x/2&&cy>=extent.y/2&&cx<=i32(p.source.x)-(extent.x+1)/2&&cy<=i32(p.source.y)-(extent.y+1)/2;
            let first=vec2<i32>(position)-extent/2;var means=vec4<f32>(0.0);var gradient=0.0;
            if valid {for(var y=0;y<extent.y;y++) {for(var x=0;x<extent.x;x++) {let pos=vec2<u32>(first+vec2<i32>(x,y));let available=aux(pos);if available.y==0.0||available.z!=0.0 {valid=false;break;}
                let weight=fields[at(vec2<u32>(u32(x),u32(y)),p.field)].x;means+=src(pos)*weight;gradient+=available.x*weight;}if !valid {break;}}}
            means/=max(count,1.0);gradient/=max(count,1.0);let normalized=(means.rgb-target_value.rgb)/vec3<f32>(8.0,6.0,6.0);let color=dot(normalized,normalized);
            valid=valid&&color<=4.0&&abs(means.a-target_value.a)<=.08;
            let score=color+pow((means.a-target_value.a)/.05,2.0)+pow((gradient-target_g)/.02,2.0);
            value=vec4<f32>(score,vec2<f32>(first),select(0.0,1.0,valid));
        }
        case 9u:{var best=3.4e38;var pos=vec2<f32>(0.0);var valid=false;
            for(var y=0u;y<p.source.y;y++) {for(var x=0u;x<p.source.x;x++) {let s=src(vec2<u32>(x,y));if s.a>0.0&&s.x<best {best=s.x;pos=s.yz;valid=true;}}}
            value=select(vec4<f32>(select(0.0,1.0,valid),0.0,0.0,0.0),vec4<f32>(pos,p.args[0].xy),q.x==0u);}
        case 10u:{let distance=src(q).x;let healthy=aux(q+vec2<u32>(p.args[0].xy)).y;value=vec4<f32>(select(0.0,1.0,distance>p.args[0].z&&distance<=p.args[0].w&&healthy>0.0));}
        case 11u:{
            var mean=vec3<f32>(0.0);var count=0.0;var target_sum=0.0;var donor_sum=0.0;var good=true;
            for(var y=0u;y<p.source.y;y++) {for(var x=0u;x<p.source.x;x++) {let pos=vec2<u32>(x,y);let mask=extra[at(pos,p.extra)];let background=aux(pos);
                if mask.y>0.0&&background.a<=1e-6 {good=false;}if mask.x>0.0 {mean+=fields[at(pos,p.field)].rgb;count+=1.0;}}}
            mean/=max(count,1.0);
            for(var y=0u;y<p.source.y;y++) {for(var x=0u;x<p.source.x;x++) {let pos=vec2<u32>(x,y);if extra[at(pos,p.extra)].x>0.0 {let t=src(pos).rgb-aux(pos).rgb;let donor=fields[at(pos,p.field)].rgb-mean;target_sum+=dot(t,t);donor_sum+=dot(donor,donor);}}}
            let target_rms=sqrt(target_sum/max(count*3.0,1.0));let donor_rms=sqrt(donor_sum/max(count*3.0,1.0));
            value=select(vec4<f32>(target_rms,donor_rms,clamp(target_rms/max(donor_rms,1e-6),.5,1.5),select(0.0,1.0,good&&count>=8.0)),vec4<f32>(mean,count),q.x==0u);
        }
        case 12u:{let target_value=src(q);let mean=extra[p.extra.w].rgb;let status=extra[p.extra.w+1u];
            if status.a>0.0 {let repaired=aux(q).rgb+(fields[at(q,p.field)].rgb-mean)*status.z;value=vec4<f32>((repaired-target_value.rgb)*target_value.a,target_value.a);}}
        case 13u:{value=src(q);let donor=aux(vec2<u32>(0u));let valid=aux(vec2<u32>(1u)).x>0.0&&fields[p.field.w+1u].a>0.0;
            if valid&&all(vec2<f32>(q)>=donor.xy)&&all(vec2<f32>(q)<donor.xy+donor.zw) {value=vec4<f32>(1.0);}}
        case 14u:{let outward=src(q).x;let skin=aux(q+vec2<u32>(p.args[0].xy)).x;value=vec4<f32>((1.0-smooth01(outward/p.args[0].z))*clamp(skin/.8,0.0,1.0));}
        case 15u:{let v=src(q);value=vec4<f32>(v.rgb/max(v.a,1.0),min(1.0,v.a));}
        case 16u:{let lab=fields[at(q,p.field)].rgb;let coarse=extra[at(q,p.extra)].rgb;let healthy=aux(q).x>.8&&src(q).x<.05&&all(abs(lab.yz-coarse.yz)<vec2<f32>(4.0));value=vec4<f32>(src(q).x,select(0.0,1.0,healthy),0.0,0.0);}
        case 17u:{value=src(vec2<u32>(2u,u32(p.args[0].x)));if q.x==1u {value=vec4<f32>(src(vec2<u32>(1u,u32(p.args[0].x))).w,src(vec2<u32>(3u,u32(p.args[0].x))).z,0.0,0.0);}if q.y==1u {value=src(vec2<u32>(1u,u32(p.args[0].x)));}}
        case 18u:{let pixel=src(q);let mean=aux(vec2<u32>(0u)).x;let threshold=max(.02,2.5*fields[p.field.w].x);value=vec4<f32>(select(0.0,1.0,pixel.x<mean-threshold&&pixel.y>.8&&pixel.z<.05));}
        case 19u:{let v=src(q);value=vec4<f32>(v.x,v.y,aux(q).x,v.w);}
        case 20u:{let v=src(q);value=vec4<f32>(select(v.rgb,v.rgb/v.a,v.a>1e-6),v.a);}
        case 21u:{value=vec4<f32>(src(q).x,0.0,0.0,select(0.0,1.0,aux(q).x>.8));}
        case 22u:{value=src(vec2<u32>(0u,u32(p.args[0].x)));}
        case 23u:{value=src(q);if aux(q).x!=0.0 {value.y=0.0;}}
        default:{value=src(q)+aux(q)*data[0];}
    }
    result[k]=value;
}
