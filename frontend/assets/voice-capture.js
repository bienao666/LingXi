/* Only runs after an explicit microphone request. No network or persistent state. */
class LingxiCapture extends AudioWorkletProcessor {
  constructor() {super();this.block=new Float32Array(1024);this.used=0;this.port.onmessage=()=>{if(this.used)this.port.postMessage(this.block.slice(0,this.used));this.used=0;};}
  process(inputs) {
    const channel=inputs[0]?.[0];
    if(channel)for(const value of channel){this.block[this.used++]=value;if(this.used===this.block.length){this.port.postMessage(this.block);this.block=new Float32Array(1024);this.used=0;}}
    return true;
  }
}
registerProcessor('lingxi-capture',LingxiCapture);
