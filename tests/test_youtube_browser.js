const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const stub = new Proxy(() => {}, { get: () => stub });
const id = 'asAB_rIf-GY';
const url = `https://www.youtube.com/watch?v=${id}`;
const format = {itag:18, height:360, mimeType:'video/mp4; codecs="avc1, mp4a"',
  audioQuality:'AUDIO_QUALITY_MEDIUM', url:'https://r1.googlevideo.com/videoplayback?id=test', contentLength:'1000000'};
const response = {videoDetails:{videoId:id,title:'测试标题'}, streamingData:{formats:[format]}};
let injected = 0;
const chrome = Object.create(stub);
chrome.tabs = {get:async () => ({url}), onRemoved:stub};
const context = vm.createContext({chrome, URL, navigator:{userAgent:'test'},
  window:{ytInitialPlayerResponse:{videoDetails:{videoId:'other'}}},
  document:{querySelectorAll:() => [{src:'https://www.youtube.com/s/player/test/player_embed.vflset/en_US/base.js'}], querySelector:selector => selector === 'ytd-watch-flexy' ? {playerData:response} : null}});
chrome.scripting = {executeScript:async ({args,world}) => {
  assert.equal(world, 'MAIN'); injected++;
  return [{result:context.readYouTubePlayer(...args)}];
}};
vm.runInContext(fs.readFileSync('browser_extension/background.js','utf8'),context);
(async () => {
  const result = await context.browserYouTube(url, 1);
  assert.equal(result.formats[0].format_id, 'browser:18');
  assert.equal(result.title,'测试标题');
  let sent;
  context.sendToApp = async (link,opts) => { sent={link,opts}; return {ok:true}; };
  await context.downloadMedia({kind:'page',url,title:'旧标题'}, {format_id:'browser:18'}, 1);
  assert.equal(sent.link,format.url);
  assert.equal(sent.opts.filename,'测试标题.mp4');
  assert.equal(sent.opts.cookie,'');
  assert.equal(sent.opts.media_type,'http');
  assert.match(sent.opts.browser_player_url,/base\.js$/);
  const before = injected;
  await context.browserYouTube('https://www.youtube.com/watch?v=WuYJWGzh0uw',1);
  assert.equal(injected,before, 'Must not inspect a different video');
  const data = context.readYouTubePlayer(id);
  for (const bad of ['https://evil.test/videoplayback', 'http://r1.googlevideo.com/videoplayback',
    'https://r1.googlevideo.com/videoplayback?range=0-999', 'https://r1.googlevideo.com/videoplayback?sq=1']) {
    assert.equal(context.normalizeYouTubeCapture({...data,formats:[{...data.formats[0],url:bad}]},id).formats.length,0);
  }
  response.streamingData = {adaptiveFormats:[format], serverAbrStreamingUrl:'https://r1.googlevideo.com/videoplayback'};
  const sabr = await context.browserYouTube(url,1);
  assert.equal(sabr.formats.length,0,'Must not present an adaptive track as a complete video');
  assert.match(sabr.diagnostic,/SABR/);
  response.streamingData = {adaptiveFormats:[
    {...format,itag:137,height:1080},
    {...format,itag:140,height:undefined,mimeType:'audio/mp4',bitrate:128000}
  ]};
  const hd = await context.browserYouTube(url,1);
  assert.equal(hd.formats[0].height,1080);
  assert.equal(hd.formats[0].needs_ffmpeg,true);
  await context.downloadMedia({kind:'page',url,title:'video'},{best:true},1);
  assert.equal(sent.opts.browser_pair.length,2);
  assert.equal(sent.opts.media_type,'page');
  console.log('YouTube browser extraction, download routing and boundary checks passed');
})().catch(error => {console.error(error);process.exitCode=1;});
