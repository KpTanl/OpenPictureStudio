struct Params {
    output:vec4<u32>, source:vec4<u32>, auxiliary:vec4<u32>, field:vec4<u32>, extra:vec4<u32>,
    args:array<vec4<f32>,8>
};
@group(0) @binding(0) var<storage,read> source:array<vec4<f32>>;
@group(0) @binding(1) var<storage,read> auxiliary:array<vec4<f32>>;
@group(0) @binding(2) var<storage,read> fields:array<vec4<f32>>;
@group(0) @binding(3) var<storage,read> extra:array<vec4<f32>>;
@group(0) @binding(4) var<storage,read_write> result:array<vec4<f32>>;
@group(0) @binding(5) var<storage,read> data:array<f32>;
@group(0) @binding(6) var<uniform> p:Params;
fn at(q:vec2<u32>,shape:vec4<u32>)->u32 { return shape.w+min(q.y,shape.y-1u)*shape.z+min(q.x,shape.x-1u); }
fn lum(v:vec3<f32>)->f32 { return v.r*0.2126+v.g*0.7152+v.b*0.0722; }
fn lut(v:f32,offset:u32,count:u32)->f32 {
    if count==0u { return v; }
    let x=clamp(v,0.0,1.0)*f32(count-1u); let i=u32(x); let t=x-f32(i);
    return data[offset+i]*(1.0-t)+data[offset+min(i+1u,count-1u)]*t;
}
fn encode(v:vec3<f32>)->vec3<f32> {
    return clamp(select(1.055*pow(max(v,vec3<f32>(0.0)),vec3<f32>(1.0/2.4))-0.055,
        v*12.92,v<=vec3<f32>(0.0031308)),vec3<f32>(0.0),vec3<f32>(1.0));
}
fn decode(v:vec3<f32>)->vec3<f32> {
    return select(pow(max((v+0.055)/1.055,vec3<f32>(0.0)),vec3<f32>(2.4)),v/12.92,v<=vec3<f32>(0.04045));
}
fn transfer(v:vec3<f32>,inverse:bool)->vec3<f32> {
    var out:vec3<f32>;
    for(var c=0u;c<3u;c++) {
        out[c]=lut(v[c],0u,4096u);
        if v[c]<=0.0 { out[c]=0.0; }
        if v[c]>=1.0 { out[c]=select(1.0,pow((v[c]+0.055)/1.055,2.4),inverse); }
    }
    return out;
}
fn balance(v:vec3<f32>,t:f32,tint:f32)->vec3<f32> {
    let gains=exp2(vec3<f32>(t*1.35,-tint*0.8,-t*1.35)/100.0);
    return v*(gains/max(lum(gains),0.0001));
}
fn shoulder(v:f32,scale:f32)->f32 {
    let y=v*scale; if y<=0.75 { return y; }
    let white=sqrt(scale); let k=(white-1.0)/(0.25*(white-0.75));
    return min(0.75+(y-0.75)/(1.0+(y-0.75)*k),1.0);
}
fn expose(v:vec3<f32>,ev:f32,version:u32)->vec3<f32> {
    if ev==0.0 || (version==1u && abs(ev)<0.000001) { return v; }
    let scale=exp2(clamp(ev,-10.0,10.0)); let y=v*scale;
    if version==1u { return select(y,0.75+0.7*(1.0-exp(-(y-0.75)/0.7)),y>vec3<f32>(0.75)); }
    if scale<=1.0 { return y; }
    let clipped=clamp(v,vec3<f32>(0.0),vec3<f32>(1.0));
    let high=max(max(clipped.r,clipped.g),clipped.b); let low=min(min(clipped.r,clipped.g),clipped.b);
    var ratio=vec3<f32>(0.0); if high>low { ratio=(clipped-low)/(high-low); }
    let a=shoulder(low,scale); return a+(shoulder(high,scale)-a)*ratio;
}
fn hue(v:vec3<f32>,epsilon:f32)->vec3<f32> {
    let hi=max(max(v.r,v.g),v.b); let lo=min(min(v.r,v.g),v.b); let d=hi-lo; let l=(hi+lo)*0.5;
    if d<epsilon { return vec3<f32>(0.0,0.0,l); }
    var h=60.0*((v.r-v.g)/d+4.0);
    if abs(hi-v.r)<epsilon { h=60.0*((v.g-v.b)/d); }
    else if abs(hi-v.g)<epsilon { h=60.0*((v.b-v.r)/d+2.0); }
    h=h-floor(h/360.0)*360.0;
    let denominator=1.0-abs(2.0*l-1.0); var s=0.0;
    if denominator>=epsilon { s=clamp(d/denominator,0.0,1.0); }
    return vec3<f32>(h,s,l);
}
fn rgb(hsl:vec3<f32>)->vec3<f32> {
    let c=(1.0-abs(2.0*hsl.z-1.0))*hsl.y; let h=hsl.x/60.0;
    let x=c*(1.0-abs((h-2.0*floor(h/2.0))-1.0)); var v:vec3<f32>;
    switch u32(floor(h))%6u {
        case 0u:{v=vec3<f32>(c,x,0.0);} case 1u:{v=vec3<f32>(x,c,0.0);}
        case 2u:{v=vec3<f32>(0.0,c,x);} case 3u:{v=vec3<f32>(0.0,x,c);}
        case 4u:{v=vec3<f32>(x,0.0,c);} default:{v=vec3<f32>(c,0.0,x);}
    }
    return clamp(v+(hsl.z-c*0.5),vec3<f32>(0.0),vec3<f32>(1.0));
}
fn mix_colors(v:vec3<f32>,count:u32)->vec3<f32> {
    let hsl=hue(v,0.0000001); let h=hsl.x; let s=hsl.y; let l=hsl.z;
    if count==0u || s<0.000001 { return v; }
    let t=clamp((s-0.02)/0.06,0.0,1.0); let soft=t*t*(3.0-2.0*t);
    var weights:array<f32,8>; var total=0.0;
    for(var i=0u;i<count;i++) {
        let k=i*5u; let delta=h-data[k]+180.0;
        let d=abs(delta-floor(delta/360.0)*360.0-180.0); var w=0.0;
        if d<data[k+1u] { let cs=cos(1.5707963267948966*(d/data[k+1u])); w=cs*cs*soft; }
        weights[i]=w; total+=w;
    }
    if total<=0.000001 { return v; }
    var change=vec3<f32>(0.0);
    for(var i=0u;i<count;i++) {
        let w=weights[i]/max(total,1.0); if w<=0.0000001 { continue; }
        let k=i*5u; let sat=data[k+3u]; let light=data[k+4u];
        change+=w*vec3<f32>(data[k+2u]/100.0*45.0,sat/100.0*select(s,1.0-s,sat>0.0),light/100.0*select(l,1.0-l,light>0.0));
    }
    let nh=h+change.x; return rgb(vec3<f32>(nh-floor(nh/360.0)*360.0,clamp(s+change.y,0.0,1.0),clamp(l+change.z,0.0,1.0)));
}
fn equal_gain(v:vec3<f32>,l:f32,desired:f32)->vec3<f32> {
    let gain=desired/max(l,0.001/255.0); let blend=clamp(l/(16.0/255.0),0.0,1.0);
    return blend*(v*gain)+(1.0-blend)*(v+(desired-l));
}
fn zone(v:f32,low:f32,high:f32,amount:f32,point:bool,bright:bool)->f32 {
    if amount==0.0 || v<low || v>high { return 0.0; }
    let u=clamp((v-low)/(high-low),0.0,1.0); let d=select(u,1.0-u,bright);
    let w=clamp(select(sin(3.141592653589793*d),1.0-d,point),0.0,1.0);
    return amount/100.0*(w*w*(3.0-2.0*w))*(45.0/255.0);
}
fn gamut(v:vec3<f32>)->vec3<f32> {
    let l=lum(v); let c=v-l; var scale=1.0;
    for(var n=0u;n<3u;n++) {
        if c[n]>0.00000001 { scale=min(scale,(1.0-l)/c[n]); }
        else if c[n]<-0.00000001 { scale=min(scale,-l/c[n]); }
    }
    return l+c*clamp(scale,0.0,1.0);
}
fn mask(q:vec2<u32>,kind:u32,legacy:bool)->f32 {
    var uv=(vec2<f32>(q)+0.5)*p.args[3].xy/vec2<f32>(p.output.xy);
    if legacy { uv=vec2<f32>(q)/max(vec2<f32>(p.output.xy)-1.0,vec2<f32>(1.0)); }
    let g=p.args[0]; let v=p.args[1];
    if kind==0u {
        let d=g.zw-g.xy; let length=dot(d,d); if length<0.00000001 { return 1.0; }
        let t=clamp(dot(uv-g.xy,d)/length,0.0,1.0); return 1.0-t*t*(3.0-2.0*t);
    }
    let d=uv-g.xy; let tangle=v.x*0.017453292519943295;
    let rotated=vec2<f32>(cos(tangle)*d.x+sin(tangle)*d.y,-sin(tangle)*d.x+cos(tangle)*d.y)/g.zw;
    let radius=length(rotated); var weight=select(0.0,1.0,radius<=1.0);
    if v.y>0.0 { let t=clamp((radius-(1.0-v.y))/v.y,0.0,1.0); weight=1.0-t*t*(3.0-2.0*t); }
    return select(weight,1.0-weight,v.z!=0.0);
}
@compute @workgroup_size(8,8)
fn main(@builtin(global_invocation_id) id:vec3<u32>) {
    if any(id.xy>=p.output.xy) { return; }
    let q=id.xy; let pixel=source[at(q,p.source)]; let aux=auxiliary[at(q,p.auxiliary)];
    let field=fields[at(q,p.field)]; let ext=extra[at(q,p.extra)]; var out=pixel;
    switch p.output.z {
        case 0u: { out=vec4<f32>(transfer(expose(balance(pixel.rgb,field.x,field.y),field.z,u32(p.args[0].x)),false),pixel.a); }
        case 1u: {
            var v=pixel.rgb; let tone=u32(p.args[0].x); let bins=u32(p.args[0].y); let st=p.args[0].z; let ss=p.args[0].w;
            if st>0.0 { let l=lum(v); let adjusted=clamp(v*(lut(l,0u,tone)/max(l,0.0001)),vec3<f32>(0.0),vec3<f32>(1.0)); v=(1.0-st)*v+st*adjusted; }
            if ss>0.0 {
                let h=hue(v,0.000000000001).x; let binwidth=360.0/f32(bins); var h0=h-binwidth*0.5; if h0<0.0 { h0+=360.0; }
                let x=h0/binwidth; let a=min(u32(x),bins-1u); let t=x-f32(a); let gain=data[tone+a]*(1.0-t)+data[tone+(a+1u)%bins]*t;
                let adjusted=clamp(lum(v)+gain*(v-lum(v)),vec3<f32>(0.0),vec3<f32>(1.0)); v=(1.0-ss)*v+ss*adjusted;
            }
            out=vec4<f32>(v,pixel.a);
        }
        case 2u: {
            var v=pixel.rgb;
            if abs(field.x-1.0)>=0.0001 { let c=clamp(v,vec3<f32>(0.0),vec3<f32>(1.0)); let a=max(field.x,0.001); let d=pow(c,vec3<f32>(a)); v=d/(d+pow(1.0-c,vec3<f32>(a))+0.000000000001); }
            for(var channel=0u;channel<4u;channel++) {
                let offset=u32(p.args[0][channel]); let count=u32(p.args[1][channel]);
                if channel==0u { v=vec3<f32>(lut(v.r,offset,count),lut(v.g,offset,count),lut(v.b,offset,count)); }
                else { v[channel-1u]=lut(v[channel-1u],offset,count); }
            }
            out=vec4<f32>(v,pixel.a);
        }
        case 3u: {
            var v=mix_colors(pixel.rgb,u32(p.args[0].x)); let amount=field.y/100.0; let s=clamp(max(max(v.r,v.g),v.b)-min(min(v.r,v.g),v.b),0.0,1.0);
            let t=clamp((s-0.03)/0.09,0.0,1.0); let gain=select(1.0+amount*0.75,1.0+amount*0.6*(t*t*(3.0-2.0*t))*(1.0-s),amount>0.0);
            v=lum(v)+(v-lum(v))*gain; v=lum(v)+(v-lum(v))*field.z;
            out=vec4<f32>(v,pixel.a);
        }
        case 4u: { out=vec4<f32>(transfer(gamut(pixel.rgb),true),pixel.a); }
        case 5u: { out=vec4<f32>(lum(pixel.rgb)); }
        case 6u: {
            let original=lum(pixel.rgb); var current=original;
            if p.args[0].x!=0.0 { let b=aux.x; current=b+zone(b,0.0,128.0/255.0,field.x,false,false); current+=zone(b,128.0/255.0,1.0,field.y,false,true); current+=original-b; }
            var desired=current+zone(current,0.0,80.0/255.0,field.z,true,false); desired+=zone(current,175.0/255.0,1.0,field.w,true,true);
            out=vec4<f32>(equal_gain(pixel.rgb,original,clamp(desired,0.0,1.0)),pixel.a);
        }
        case 7u: { let k=u32(p.args[0].x); out.y+=p.args[0].y*(field[k]/100.0)*(aux.x-ext.x); }
        case 8u: {
            var v=equal_gain(pixel.rgb,aux.x,clamp(aux.y,0.0,1.0)); let l=lum(v); v=l+(v-l)*(1.0+0.10*max(field.z,0.0)/100.0);
            out=vec4<f32>(v,pixel.a);
        }
        case 9u: { out=p.args[0]; }
        case 10u: { out=pixel+aux.x*p.args[0]; }
        case 11u: { out=clamp(pixel,p.args[0],p.args[1]); }
        case 12u: { out=vec4<f32>(mask(q,u32(p.args[2].x),p.args[2].y!=0.0)); }
        case 13u: { out=vec4<f32>(0.0); }
        case 14u: { out=vec4<f32>(select(pixel.x*(1.0-aux.x),max(pixel.x,aux.x),p.args[0].x==0.0)); }
        case 15u: {
            let uv=(vec2<f32>(q)+0.5)*p.args[0].xy/vec2<f32>(p.output.xy); let radius=p.args[0].z; var distance=3.4e38;
            let count=u32(p.args[1].x); var previous=vec2<f32>(data[0],data[1]);
            for(var i=0u;i<count;i++) {
                let current=vec2<f32>(data[i*2u],data[i*2u+1u]); let d=current-previous; let rel=uv-previous;
                let t=clamp(dot(rel,d)/max(dot(d,d),0.000000000001),0.0,1.0); distance=min(distance,length(rel-t*d)); previous=current;
            }
            var w=select(0.0,1.0,distance<=radius); let hardness=p.args[0].w;
            if hardness<1.0 { let t=clamp((distance/radius-hardness)/(1.0-hardness),0.0,1.0); w=1.0-t*t*(3.0-2.0*t); }
            out=vec4<f32>(w*p.args[1].y);
        }
        case 16u: {
            let uv=clamp((vec2<f32>(q)+0.5)*vec2<f32>(p.auxiliary.xy)/vec2<f32>(p.output.xy)-0.5,vec2<f32>(0.0),vec2<f32>(p.auxiliary.xy)-1.0);
            let a=vec2<u32>(floor(uv)); let b=min(a+1u,p.auxiliary.xy-1u); let t=fract(uv);
            let top=auxiliary[at(a,p.auxiliary)].x*(1.0-t.x)+auxiliary[at(vec2<u32>(b.x,a.y),p.auxiliary)].x*t.x;
            let bottom=auxiliary[at(vec2<u32>(a.x,b.y),p.auxiliary)].x*(1.0-t.x)+auxiliary[at(b,p.auxiliary)].x*t.x;
            out=vec4<f32>(top*(1.0-t.y)+bottom*t.y);
        }
        case 17u: { out=vec4<f32>(encode(expose(balance(decode(pixel.rgb),field.x,field.y),field.z,u32(p.args[0].x))),pixel.a); }
        case 18u: { out=vec4<f32>(lum(pixel.rgb)+(pixel.rgb-lum(pixel.rgb))*field.z,pixel.a); }
        case 19u: { out=vec4<f32>((1.0-field.x)*pixel.rgb+field.x*aux.rgb,pixel.a); }
        case 21u: { out=vec4<f32>(pixel.bgr,pixel.a); }
        default: { out=vec4<f32>(decode(clamp(pixel.rgb,vec3<f32>(0.0),vec3<f32>(1.0))),pixel.a); }
    }
    result[q.y*p.output.x+q.x]=out;
}
