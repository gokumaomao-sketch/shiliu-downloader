const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('browser_extension/background.js','utf8');
const fn=source.slice(source.indexOf('function readPagePlayer()'),source.indexOf('async function browserPageMedia('));
const video={paused:false,currentTime:3,currentSrc:'https://media.example/v.mp4',
 getBoundingClientRect:()=>({width:640,height:360})};
const ctx=vm.createContext({document:{querySelectorAll:()=>[video]},window:{}});
vm.runInContext(fn,ctx);
assert.equal(ctx.readPagePlayer(),video.currentSrc);
video.currentSrc='blob:abcd';
ctx.window.jwplayer=()=>({getPlaylistItem:()=>({file:'https://media.example/v.m3u8'})});
assert.equal(ctx.readPagePlayer(),'https://media.example/v.m3u8');
console.log('PASS: player MP4 and manifest detection');
