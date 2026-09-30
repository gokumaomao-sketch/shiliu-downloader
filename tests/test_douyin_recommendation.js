const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const stub = new Proxy(() => {}, {get: () => stub});
const page = 'https://www.douyin.com/video/7637600942771383675';
let activePage = page;
const chrome = Object.create(stub);
chrome.tabs = {get: async () => ({url: activePage}), onRemoved: stub};
const ctx = vm.createContext({chrome, URL, navigator: {userAgent: 'test'}});
vm.runInContext(fs.readFileSync('browser_extension/background.js', 'utf8'), ctx);
vm.runInContext(`tabMedia.set(1, new Map([
  ['page', {url:'${page}', kind:'page', mclass:'video'}],
  ['old', {url:'https://cdn.example/old.mp4', kind:'file', mclass:'video',
    pageUrl:'https://www.douyin.com/video/111', size:9000000}],
  ['first', {url:'https://cdn.example/current.mp4', kind:'file', mclass:'video',
    pageUrl:'${page}', title:'测试短片', size:427110, ext:'mp4'}],
  ['second', {url:'https://cdn.example/alternative.mp4', kind:'file', mclass:'video',
    pageUrl:'${page}', size:220000, ext:'mp4'}]
]));`, ctx);

(async () => {
  // Model the screenshot: two sub-1MB MP4s while webpage parsing needs cookies.
  ctx.browserPageMedia = async () => null;
  ctx.probeApp = async () => { throw new Error('must not need webpage cookies'); };
  const result = await ctx.probeMedia(page, {media_type: 'page'}, 1);
  assert.equal(result.ok, true);
  assert.equal(result.formats[0].format_id, 'browser:detected');
  assert.equal(result.formats[0].filesize, 427110);
  let sent;
  ctx.sendToApp = async (url, opts) => { sent = {url, opts}; return {ok:true}; };
  await ctx.downloadMedia({kind:'page', url:page, title:'测试短片'}, {best:true}, 1);
  assert.equal(sent.url, 'https://cdn.example/current.mp4');
  assert.equal(sent.opts.media_type, 'http');
  assert.equal(sent.opts.filename, '测试短片.mp4');

  // A readable active player takes priority over size-ranked network entries.
  ctx.browserPageMedia = async () => ({url:'https://cdn.example/playing.mp4', kind:'file', ext:'mp4'});
  await ctx.downloadMedia({kind:'page', url:page}, {format_id:'browser:detected'}, 1);
  assert.equal(sent.url, 'https://cdn.example/playing.mp4');

  // An old open panel must not download a clip after the user switches videos.
  activePage = 'https://www.douyin.com/video/222';
  sent = null;
  const stale = await ctx.downloadMedia({kind:'page', url:page}, {best:true}, 1);
  assert.equal(stale.ok, false);
  assert.equal(sent, null);
  console.log('PASS: Douyin first recommendation, small MP4s, player preference and stale-page guard');
})().catch(e => { console.error(e); process.exitCode = 1; });
