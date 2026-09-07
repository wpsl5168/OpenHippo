/* Offline real Chrome regression. Exact UI methods are extracted, not reimplemented.
 * No project imports or live APIs. All fetch calls are synthetic; network is denied.
 * Run: node scripts/ui_p1_browser.cjs
 * Dependencies can be overridden using the environment variables below.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const puppeteer = require(process.env.PUPPETEER_CORE || '/home/wpsl5168/.local/share/fireworks-runtime/node_modules/puppeteer-core');
const root = path.resolve(__dirname, '..');
const vendor = process.env.UI_TEST_VENDORS || '/home/wpsl5168/work/openhippo-review/2026-09-07/vendors';
const python = process.env.UI_TEST_PYTHON || '/home/wpsl5168/work/openhippo-review/2026-09-07/test-venv/bin/python';
const sourcePath = process.env.UI_TEST_SOURCE || path.join(root, 'src/openhippo/ui/index.html');
const source = fs.readFileSync(sourcePath, 'utf8');
function methodBlock(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, 'Exact source method boundaries must exist');
  return source.slice(a, b);
}
const methods = methodBlock('    renderMarkdown(text) {', '\n  };\n}') + '\n' +
  methodBlock('    async exportAll() {', '    copy(text)');
// Syntax-check all inline scripts without running UI initialization or CDN code.
let scriptsChecked = 0;
for (const m of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
  if (m[1].trim()) { new vm.Script(m[1]); scriptsChecked++; }
}
const artifactRoot = path.join(__dirname, 'ui-p1-artifacts');
fs.mkdirSync(artifactRoot, {recursive:true});
const output = fs.mkdtempSync(path.join(artifactRoot, 'run-'));
const fixture = process.env.UI_TEST_FIXTURE
  ? fs.readFileSync(process.env.UI_TEST_FIXTURE, 'utf8')
  : execFileSync(python, [path.join(__dirname, 'ui_p1_synthetic.py'), 'export-fixture'], {encoding:'utf8'});
fs.writeFileSync(path.join(output, 'export-fixture.json'), fixture);

(async () => {
  const browser = await puppeteer.launch({executablePath:process.env.CHROME_BIN || '/usr/bin/google-chrome',
    headless:true, userDataDir:path.join(output, 'chrome-profile'),
    args:['--disable-dev-shm-usage', '--disable-background-networking']});
  const result = {scope:'isolated sandboxed Chrome, exact UI source methods, local matching vendors, fake fetch only, real disk downloads',
    output, source:sourcePath, browser:await browser.version(), inline_scripts_syntax_checked:scriptsChecked, cases:[]};
  try {
    const page = await browser.newPage();
    const requests = [];
    await page.setRequestInterception(true);
    page.on('request', r => { requests.push(r.url()); r.abort(); });
    await page.goto('about:blank');
    await page.addScriptTag({path:path.join(vendor, 'marked.min.js')});
    await page.addScriptTag({path:path.join(vendor, 'purify.min.js')});
    await page.evaluate(methods => {
      window.ui = new Function('return ({' + methods + '})')();
      window.__AUDIT_XSS = 0;
      window.notices = [];
      ui.notify = (message, type) => notices.push({message, type});
      window.fetch = async () => { throw Error('Unexpected fetch'); };
      window.put = (text, query) => {
        document.body.replaceChildren();
        const before = document.createElement('div');
        before.innerHTML = ui.renderMarkdown(text);
        const after = document.createElement('div');
        after.innerHTML = ui.renderMd(text, query);
        document.body.appendChild(after);
        return {before, after};
      };
    }, methods);

    // Persistent handlers would still execute if sanitization is undone by highlighting.
    const attacks = [
      '&lt;img src=x onerror=window.__AUDIT_XSS=1&gt; needle',
      '&lt;svg onload=window.__AUDIT_XSS=2&gt;&lt;/svg&gt; needle',
      '&#60;img src=x onerror=window.__AUDIT_XSS=3&#62; needle',
      '<img src=x onerror=window.__AUDIT_XSS=4> needle',
      '[needle](javascript:window.__AUDIT_XSS=5)',
      '&lt;img src=x onerror=window.__AUDIT_XSS=6&gt; <b>needle</b>',
      'needle &amp;lt;img src=x onerror=window.__AUDIT_XSS=7&amp;gt;'
    ];
    for (const [i, payload] of attacks.entries()) {
      const check = await page.evaluate(payload => {
        const {before, after} = put(payload, 'needle');
        const badAttrs = [...after.querySelectorAll('*')].flatMap(el => [...el.attributes])
          .filter(a => /^on/i.test(a.name) || (['href','src'].includes(a.name) && /^javascript:/i.test(a.value)));
        return {beforeText:before.textContent, afterText:after.textContent,
          beforeImages:before.querySelectorAll('img, svg').length,
          afterImages:after.querySelectorAll('img, svg').length,
          badAttributes:badAttrs.length, marks:after.querySelectorAll('mark.hippo-mark').length};
      }, payload);
      await new Promise(r => setTimeout(r, 120));
      check.synthetic_event_executed = await page.evaluate(() => __AUDIT_XSS !== 0);
      result.last_security_check = check;
      assert.equal(check.beforeText, check.afterText);
      assert.equal(check.beforeImages, check.afterImages);
      assert.equal(check.badAttributes, 0);
      assert.equal(check.marks, 1);
      assert.equal(check.synthetic_event_executed, false);
      result.cases.push({name:`xss-${i+1}`, status:'PASS', ...check});
    }
    const markdown = await page.evaluate(() => {
      const {before, after} = put('# Heading\n\n**Needle** and *needle* and [needle](https://example.invalid/needle "needle")\n\n- needle\n- item\n\n`needle`\n\n```js\nneedle\n```\n\n<pre><span>needle</span></pre>', 'needle');
      return {textUnchanged:before.textContent===after.textContent,
        elements:['h1','strong','em','a','ul','li','code','pre'].map(tag => [tag,before.querySelectorAll(tag).length,after.querySelectorAll(tag).length]),
        markCount:after.querySelectorAll('mark.hippo-mark').length,
        codeMarks:after.querySelectorAll('code mark, pre mark').length,
        link:after.querySelector('a').getAttribute('href'),title:after.querySelector('a').title};
    });
    assert(markdown.textUnchanged);
    for (const [tag, before, after] of markdown.elements) assert.equal(before,after,tag);
    assert.equal(markdown.markCount,4);
    assert.equal(markdown.codeMarks,0);
    assert.equal(markdown.link,'https://example.invalid/needle');
    assert.equal(markdown.title,'needle');
    result.cases.push({name:'markdown-and-multiple-text-nodes',status:'PASS', ...markdown});

    for (const query of ['alpha beta', 'a+b', '[x]', '.*', '(foo)', '$^', '{a}', 'a|b', 'a\\b', '<img', '中文']) {
      const check = await page.evaluate(query => {
        const text = 'before ' + query + ' middle ' + query.toUpperCase() + ' after';
        // Encode only the fixture text, leaving Markdown/HTML interpretation out of literal-query tests.
        const html = document.createElement('div'); html.textContent = text;
        const rendered = document.createElement('div'); rendered.innerHTML=ui.applyHighlight(html.innerHTML,query);
        return {textUnchanged:rendered.textContent===text, marks:[...rendered.querySelectorAll('mark')].map(n=>n.textContent)};
      }, query);
      assert(check.textUnchanged); assert.equal(check.marks.length,2);
      assert.equal(check.marks[0],query); assert.equal(check.marks[1],query.toUpperCase());
      result.cases.push({name:'literal-query:'+query,status:'PASS'});
    }
    const repeated = await page.evaluate(() => {
      let html=ui.renderMarkdown('alpha beta alpha **beta**');
      html=ui.applyHighlight(html,'alpha'); html=ui.applyHighlight(html,'beta'); html=ui.applyHighlight(html,'alpha');
      const el=document.createElement('div'); el.innerHTML=html;
      return {text:el.textContent.trim(),count:el.querySelectorAll('mark').length,nested:el.querySelectorAll('mark mark').length};
    });
    assert.deepEqual(repeated,{text:'alpha beta alpha beta',count:4,nested:0});
    result.cases.push({name:'multiple-keywords-and-idempotence',status:'PASS',...repeated});
    const noops=await page.evaluate(() => ['', 'a', 'absent'].map(q=>{
      const html=ui.renderMarkdown('hello **world**');return ui.applyHighlight(html,q)===html;
    }));
    assert(noops.every(Boolean));
    result.cases.push({name:'empty-short-unmatched-query',status:'PASS'});

    const session = await page.createCDPSession();
    const downloads = [];
    session.on('Browser.downloadWillBegin',event=>downloads.push(event));
    async function doExport({body, status=200, failure=null}) {
      return page.evaluate(async fixture => {
        notices.length=0;window.fetchCalls=[];
        window.fetch=async url=>{
          fetchCalls.push(url);
          if(fixture.failure) throw Error(fixture.failure);
          return new Response(fixture.body,{status:fixture.status,headers:{'Content-Type':'application/json'}});
        };
        await ui.exportAll();
        return {notices:[...notices],calls:[...fetchCalls],anchors:document.querySelectorAll('a[download]').length};
      },{body,status,failure});
    }
    async function verifyDownload(name, body) {
      const dir=path.join(output,name);fs.mkdirSync(dir);
      await session.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:dir,eventsEnabled:true});
      const before=downloads.length;
      const check=await doExport({body});
      assert.deepEqual(check.calls,['/v1/export?format=json&include_embeddings=true']);
      assert.equal(check.notices.length,1);assert.equal(check.notices[0].type,'ok');
      assert(!check.notices[0].message.includes('导出完成'));assert.equal(check.anchors,0);
      let files=[];
      const deadline=Date.now()+10000;
      while(Date.now()<deadline) {
        files=fs.readdirSync(dir).filter(f=>f.endsWith('.json'));
        if(files.length===1)break;
        await new Promise(r=>setTimeout(r,50));
      }
      assert.equal(files.length,1,'Chrome must produce a real JSON file');
      const filename=path.join(dir,files[0]);
      const actual=fs.readFileSync(filename,'utf8');
      assert.equal(actual,body,'Export must preserve the entire response text');
      assert.deepEqual(JSON.parse(actual),JSON.parse(body));
      result.cases.push({name,status:'PASS',download:filename,bytes:fs.statSync(filename).size,
        memory_count:JSON.parse(actual).memories.length,notices:check.notices,downloadEvents:downloads.length-before});
    }
    await verifyDownload('real-exporter-json',fixture);
    const extended=JSON.parse(fixture);
    extended.header.future_metadata={preserve:true};extended.memories[1].future_field={foo:['保留',null]};
    // JS re-serialization would corrupt this JSON integer; byte equality detects that regression.
    const extendedText=JSON.stringify(extended,null,2).replace('"preserve": true','"preserve": true, "large_integer": 900719925474099312345');
    await verifyDownload('unknown-fields-and-integer-precision',extendedText);
    await verifyDownload('empty-export',JSON.stringify({header:{schema_version:'1.0',total_count:0},memories:[]}));

    const errors=[
      {name:'http-500-json',status:500,body:fixture},
      {name:'http-401',status:401,body:'{"detail":"unauthorized"}'},
      {name:'http-403-html',status:403,body:'<html>denied</html>'},
      {name:'network-failure',failure:'synthetic network failure',body:''},
      {name:'invalid-json',body:'undefined'},
      {name:'truncated-json',body:fixture.slice(0,-10)},
      {name:'wrong-envelope',body:JSON.stringify({data:JSON.parse(fixture)})},
      {name:'null-document',body:'null'},
      {name:'missing-header',body:'{"memories":[]}'},
      {name:'invalid-header',body:'{"header":[],"memories":[]}'},
      {name:'missing-memories',body:'{"header":{"schema_version":"1.0","total_count":0}}'},
      {name:'wrong-memories-type',body:'{"header":{"schema_version":"1.0","total_count":0},"memories":{}}'},
      {name:'missing-count',body:'{"header":{"schema_version":"1.0"},"memories":[]}'},
      {name:'count-mismatch',body:'{"header":{"schema_version":"1.0","total_count":1},"memories":[]}'}
    ];
    const errorDir=path.join(output,'failed-exports');fs.mkdirSync(errorDir);
    await session.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:errorDir,eventsEnabled:true});
    const beforeErrors=downloads.length;
    for (const fixture of errors) {
      const check=await doExport(fixture);
      assert.equal(check.notices.length,1);assert.equal(check.notices[0].type,'err');
      if(fixture.status) assert(check.notices[0].message.includes('HTTP '+fixture.status));
      assert.equal(check.anchors,0);
      result.cases.push({name:fixture.name,status:'PASS',notices:check.notices});
    }
    await new Promise(r=>setTimeout(r,1200));
    assert.equal(downloads.length,beforeErrors,'Failed exports must not initiate downloads');
    assert.deepEqual(fs.readdirSync(errorDir),[]);
    assert.equal(await page.evaluate(()=>__AUDIT_XSS),0);
    result.network_requests_aborted=requests;
    result.total_passed=result.cases.filter(c=>c.status==='PASS').length;
    result.total_cases=result.cases.length;
    assert.equal(result.total_passed,result.total_cases);
    result.status='PASS';
  } catch(error) {
    result.status='FAIL';result.error=error.stack;process.exitCode=1;
  } finally {
    await browser.close();
    fs.rmSync(path.join(output,'chrome-profile'),{recursive:true,force:true});
    fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
    fs.writeFileSync(path.join(artifactRoot,'latest-result.json'),JSON.stringify(result,null,2)+'\n');
    console.log(JSON.stringify(result,null,2));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
