const svg=document.querySelector('#avatar'),head=document.querySelector('#head'),face=document.querySelector('#face'),neck=document.querySelector('#neck'),torso=document.querySelector('#torso');
const eyeL=document.querySelector('#eyeL'),eyeR=document.querySelector('#eyeR'),mouth=document.querySelector('#mouth'),mouthDetail=document.querySelector('#mouthDetail');
const browL=document.querySelector('#browL'),browR=document.querySelector('#browR'),leftPod=document.querySelector('#leftPod'),rightPod=document.querySelector('#rightPod');
const stateLabel=document.querySelector('#stateLabel'),visemeLabel=document.querySelector('#visemeLabel'),energyInput=document.querySelector('#energy'),moodInput=document.querySelector('#mood');
const energyOut=document.querySelector('#energyOut'),moodOut=document.querySelector('#moodOut'),cursorGaze=document.querySelector('#cursorGaze');

const VISEMES={
  REST:{w:74,h:8,round:.9,lift:0,asym:0,detail:0},
  MBP:{w:82,h:6,round:.95,lift:-1,asym:0,detail:0},
  AA:{w:78,h:60,round:.66,lift:1,asym:0,detail:0},
  E:{w:126,h:30,round:.52,lift:8,asym:0,detail:0},
  I:{w:112,h:21,round:.46,lift:5,asym:0,detail:0},
  O:{w:58,h:58,round:1,lift:0,asym:0,detail:0},
  U:{w:45,h:39,round:1,lift:2,asym:0,detail:0},
  FV:{w:92,h:24,round:.42,lift:1,asym:7,detail:1},
  LTD:{w:86,h:42,round:.55,lift:2,asym:-3,detail:2}
};
const speechSequence=['MBP','AA','LTD','E','FV','I','O','U','AA','E','REST'];
const state={mode:'idle',viseme:'REST',manualViseme:false,energy:.65,mood:.25,gazeX:0,gazeY:0,targetGazeX:0,targetGazeY:0,blink:0,blinkTarget:0,nextBlink:performance.now()+1200,headX:0,headY:0,headRot:0,visemeShape:{...VISEMES.REST},interruptUntil:0};
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v)),lerp=(a,b,t)=>a+(b-a)*t;
const ease=(current,target,speed,dt)=>lerp(current,target,1-Math.exp(-speed*dt));

function mouthPath(s){
  const cx=380,cy=370,w=s.w,h=Math.max(4,s.h),x0=cx-w/2,x1=cx+w/2,y0=cy-h/2,y1=cy+h/2;
  const r=clamp(s.round,0,1),lift=s.lift,as=s.asym;
  const topCtl=w*(.18+.18*r),side=h*(.18+.22*r);
  return `M ${x0} ${cy+lift-as*.16} C ${x0+topCtl} ${y0-lift}, ${x1-topCtl} ${y0-lift}, ${x1} ${cy+lift+as*.16} C ${x1-side} ${y1+lift}, ${x0+side} ${y1+lift}, ${x0} ${cy+lift-as*.16} Z`;
}
function setMouthDetail(detail,s){
  if(detail===1){mouthDetail.setAttribute('d',`M ${380-s.w*.32} 365 Q 380 373 ${380+s.w*.32} 365`);mouthDetail.style.opacity='.72'}
  else if(detail===2){mouthDetail.setAttribute('d',`M 380 ${370-s.h*.28} L 380 ${370+s.h*.16}`);mouthDetail.style.opacity='.72'}
  else{mouthDetail.setAttribute('d','');mouthDetail.style.opacity='0'}
}
function setState(mode){
  state.mode=mode;state.manualViseme=false;stateLabel.textContent=mode.toUpperCase();
  document.body.classList.toggle('error-mode',mode==='error');
  document.querySelectorAll('#stateButtons button').forEach(b=>b.classList.toggle('active',b.dataset.state===mode));
  if(mode==='interrupted'){state.interruptUntil=performance.now()+620;state.viseme='REST';}
}
function setViseme(name,manual=true){if(!VISEMES[name])return;state.viseme=name;state.manualViseme=manual;visemeLabel.textContent='viseme: '+name;
  document.querySelectorAll('#visemeButtons button').forEach(b=>b.classList.toggle('active',b.dataset.viseme===name));}

Object.keys(VISEMES).forEach(name=>{const b=document.createElement('button');b.textContent=name;b.dataset.viseme=name;b.onclick=()=>setViseme(name,true);document.querySelector('#visemeButtons').appendChild(b)});
document.querySelectorAll('#stateButtons button').forEach(b=>b.onclick=()=>setState(b.dataset.state));
energyInput.oninput=()=>{state.energy=+energyInput.value;energyOut.value=state.energy.toFixed(2)};
moodInput.oninput=()=>{state.mood=+moodInput.value;moodOut.value=state.mood.toFixed(2)};
svg.addEventListener('pointermove',e=>{if(!cursorGaze.checked)return;const r=svg.getBoundingClientRect();state.targetGazeX=clamp(((e.clientX-r.left)/r.width-.5)*2,-1,1);state.targetGazeY=clamp(((e.clientY-r.top)/r.height-.5)*2,-1,1)});
svg.addEventListener('pointerleave',()=>{state.targetGazeX=0;state.targetGazeY=0});

let last=performance.now(),speechClock=0,lastSpeechIndex=-1;
function animate(now){
  const dt=Math.min(.04,(now-last)/1000);last=now;const t=now/1000;
  state.gazeX=ease(state.gazeX,state.targetGazeX,8,dt);state.gazeY=ease(state.gazeY,state.targetGazeY,8,dt);

  if(now>state.nextBlink&&state.blinkTarget===0){state.blinkTarget=1;state.nextBlink=now+1800+Math.random()*3200}
  state.blink=ease(state.blink,state.blinkTarget,state.blinkTarget?34:21,dt);if(state.blinkTarget===1&&state.blink>.88)state.blinkTarget=0;

  let hx=0,hy=0,rot=0,brow=-state.mood*7,eyeScale=1;
  if(state.mode==='idle'){hx=Math.sin(t*.65)*2;hy=Math.sin(t*.9)*2;rot=Math.sin(t*.55)*1.4}
  if(state.mode==='listening'){hx=state.gazeX*5;hy=2+state.gazeY*3;rot=state.gazeX*2.7;brow=-3}
  if(state.mode==='thinking'){state.targetGazeX=.42;state.targetGazeY=-.55;hx=3;hy=-2;rot=-5;brow=-7}
  if(state.mode==='speaking'){
    speechClock+=dt*(4.4+state.energy*3.4);const i=Math.floor(speechClock)%speechSequence.length;
    if(!state.manualViseme&&i!==lastSpeechIndex){setViseme(speechSequence[i],false);lastSpeechIndex=i}
    hx=Math.sin(t*2.8)*3.2*state.energy;hy=Math.sin(t*5.2)*1.7*state.energy;rot=Math.sin(t*1.9)*2.1*state.energy;brow=-2-state.mood*5;
  }
  if(state.mode==='interrupted'||now<state.interruptUntil){eyeScale=1.22;hy=-5;rot=state.gazeX*2;brow=-11;state.targetGazeX=0;state.targetGazeY=0;setViseme('REST',false)}
  if(state.mode==='error'){rot=Math.sin(t*10)*1.5;brow=8;eyeScale=.82;setViseme('REST',false)}
  if(state.mode!=='speaking'&&!state.manualViseme&&state.mode!=='interrupted')setViseme('REST',false);

  state.headX=ease(state.headX,hx,6,dt);state.headY=ease(state.headY,hy,6,dt);state.headRot=ease(state.headRot,rot,7,dt);
  head.setAttribute('transform',`translate(${state.headX} ${state.headY}) rotate(${state.headRot} 380 320)`);
  neck.setAttribute('transform',`translate(0 ${-state.headY*.38}) scale(1 ${1+Math.abs(state.headY)*.002})`);
  torso.setAttribute('transform',`translate(0 ${Math.sin(t*.8)*1.2})`);
  leftPod.setAttribute('transform',`rotate(${-state.headRot*.65} 176 290)`);rightPod.setAttribute('transform',`rotate(${-state.headRot*.65} 584 290)`);

  const eyeH=Math.max(4,106*(1-state.blink*.94)*eyeScale),eyeW=72*(1+(eyeScale-1)*.35),gx=state.gazeX*11,gy=state.gazeY*8;
  for(const [el,cx] of [[eyeL,296],[eyeR,464]]){el.setAttribute('x',(cx-eyeW/2+gx).toFixed(2));el.setAttribute('y',(279-eyeH/2+gy).toFixed(2));el.setAttribute('width',eyeW.toFixed(2));el.setAttribute('height',eyeH.toFixed(2));el.setAttribute('rx',Math.min(36,eyeH/2).toFixed(2))}
  browL.setAttribute('transform',`translate(${gx*.28} ${gy*.18}) rotate(${brow} 295 201)`);browR.setAttribute('transform',`translate(${gx*.28} ${gy*.18}) rotate(${-brow} 465 201)`);

  const target={...VISEMES[state.viseme]};
  if(state.mode==='speaking'){const pulse=.9+Math.sin(t*15)*.1*state.energy;target.h*=pulse;target.w*=1+Math.sin(t*9)*.025*state.energy}
  for(const k of ['w','h','round','lift','asym'])state.visemeShape[k]=ease(state.visemeShape[k],target[k],16,dt);
  state.visemeShape.detail=target.detail;
  mouth.setAttribute('d',mouthPath(state.visemeShape));setMouthDetail(target.detail,state.visemeShape);
  requestAnimationFrame(animate);
}
setState('idle');setViseme('REST',false);requestAnimationFrame(animate);
