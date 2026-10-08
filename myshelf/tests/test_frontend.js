// Run: node myshelf/tests/test_frontend.js. No browser, network or production API.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(x => x[1]).find(x => x.includes('function app()'));
const context = {console, setTimeout:()=>1, clearTimeout:()=>{}, setInterval:()=>1, clearInterval:()=>{}, confirm:()=>true};
vm.createContext(context);
vm.runInContext(script, context);
function app() {
  const ui=context.app();
  ui.loadLibrary=async()=>{};
  ui.loadRecommendations=async()=>{};
  return ui;
}
const plain = value => JSON.parse(JSON.stringify(value));
// Session history of one tab. Traversal is asynchronous in browsers, so back() only queues it
// and the test performs it with traverse(), which fires popstate like a browser does.
function browser(hash) {
  const entries=[{hash,state:null}], listeners=[];let index=0, queued=0;
  const location={get hash(){return entries[index].hash;},set hash(value){entries.splice(index+1);entries.push({hash:value,state:null});index++;}};
  const history={get state(){return entries[index].state;},get length(){return entries.length;},
    pushState(state,title,url){entries.splice(index+1);entries.push({hash:url,state});index++;},
    replaceState(state,title,url){entries[index]={hash:url,state};},
    back(){queued--;},forward(){queued++;}};
  const window={location,history,localStorage:{setItem(){},getItem(){return null;}},
    addEventListener(type,listener){if(type==='popstate')listeners.push(listener);}};
  return {window,get index(){return index;},get queued(){return queued;},
    async traverse(){
      const target=Math.max(0,Math.min(entries.length-1,index+queued));queued=0;
      if(target===index)return;
      index=target;for(const listener of listeners)await listener({state:entries[index].state});
    }};
}
function routedApp(hash, books) {
  const page=browser(hash), routeContext={...context,URLSearchParams,window:page.window};
  vm.createContext(routeContext);vm.runInContext(script,routeContext);
  const ui=routeContext.app();
  ui.api=async url=>url==='/api/books'?books:{};
  ui.loadTab=()=>{};ui.loadDetailReader=async()=>{};ui.loadReaderStatus=async()=>{};
  ui.loadRecommendations=async()=>{};ui.loadRecommendationHistory=async()=>{};
  return {ui,page};
}
// A stand-in for Alpine on one fragment of the page: it repeats <template x-for>, hides what x-show hides and evaluates
// every binding against the component, so a binding that throws fails the check. Returns the elements in page order;
// text is what x-text gives or the static text after the opening tag.
const evaluate=(ui,scope,code)=>new Function('ui','scope',`with(ui){with(scope){${code}}}`)(ui,scope);
function render(ui, fragment) {
  const tags=[...fragment.matchAll(/<(\/?)([\w-]+)((?:\s+[^\s"=>]+(?:="[^"]*")?)*)\s*>([^<]*)/g)].map(m=>({closing:!!m[1],tag:m[2],text:m[4].trim(),
    attrs:Object.fromEntries([...m[3].matchAll(/([^\s"=]+)(?:="([^"]*)")?/g)].map(a=>[a[1],a[2]??'']))}));
  const end=[], open=[], view=[];
  tags.forEach((t,at)=>{
    if(t.tag==='input') end[at]=at;
    else if(!t.closing) open.push(at);
    else {const from=open.pop();assert.equal(tags[from]?.tag,t.tag,'the fragment closes a tag it did not open');end[from]=at;}
  });
  assert.deepEqual(open,[]);
  (function walk(from,to,scope,parent) {
    for(let at=from;at<to;at=end[at]+1) {
      const {tag,attrs,text}=tags[at], value=code=>evaluate(ui,scope,`return (${code})`);
      if(attrs['x-for']) {
        const [,names,list]=attrs['x-for'].match(/^\(?(.+?)\)?\s+in\s+(.+)$/), [item,index='$index']=names.split(/\s*,\s*/);
        (value(list)||[]).forEach((entry,n)=>walk(at+1,end[at],{...scope,[item]:entry,[index]:n},parent));
        continue;
      }
      const bound=Object.fromEntries(Object.keys(attrs).filter(name=>/^(:|x-(text|show|model)$)/.test(name)).map(name=>[name,value(attrs[name])]));
      const el={ui,tag,attrs,scope,bound,parent,shown:parent.shown&&(!('x-show' in bound)||!!bound['x-show']),
        disabled:parent.disabled||!!bound[':disabled'],text:'x-text' in bound?String(bound['x-text']):text};
      view.push(el);walk(at+1,end[at],scope,el);
    }
  })(0,tags.length,{},{shown:true,disabled:false});
  return view;
}
// What a person can do with a rendered element: a hidden or disabled control does not react.
const usable=el=>assert.ok(el&&el.shown&&!el.disabled,'the control is not on the screen or is disabled');
const press=el=>{usable(el);const code=el.attrs['@click'];return evaluate(el.ui,el.scope,code.includes(';')?code:`return (${code})`);};
const fill=(el,value)=>{usable(el);evaluate(el.ui,{...el.scope,$value:value},`${el.attrs['x-model']}=$value`);};
const tick=el=>{const now=evaluate(el.ui,el.scope,`return (${el.attrs['x-model']})`);fill(el,Array.isArray(now)?[...now,el.bound[':value']]:!now);};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async () => {
  const ui=app();let sent;
  ui.editBook({id:1,title:'Old title',authors:'Author',summary:'',description:null,year:null,language:null});
  ui.form.title='Corrected title';
  ui.patchBook=async(id,body)=>{sent={id,body};};
  await ui.saveBook();
  assert.deepEqual(plain(sent),{id:1,body:{title:'Corrected title'}});
  console.log('PASS editing title does not send stale summary');

  ui.editBook({id:1,title:'Title',authors:'Author',summary:'Old review',year:2020,language:'en'});
  ui.form.summary='';ui.form.year='';ui.form.language='';
  await ui.saveBook();
  assert.deepEqual(plain(sent.body),{summary:'',year:null,language:null});
  console.log('PASS intentional clearing of review, year and language is retained');

  ui.selected={source_id:'/works/OL1W',title:'Existing book'};ui.addStatus='want';
  ui.api=async()=>({status:'exists',id:1,book_status:'read'});
  await ui.addSelected();
  assert.equal(ui.shelf,'read');
  assert.match(ui.toasts.at(-1).message,/уже в библиотеке.*ПРОЧИТАНО/);
  console.log('PASS duplicate search add displays current status');

  const recommended=app();let dismissed=false;
  recommended.api=async()=>({status:'exists',id:1,book_status:'dropped'});
  recommended.dismissRecommendation=async()=>{dismissed=true};
  await recommended.addRecommended({id:2,source_id:'/works/OL1W',title:'Existing book'});
  assert.equal(dismissed,false);
  assert.match(recommended.toasts.at(-1).message,/БРОШЕНО/);
  console.log('PASS duplicate recommendation add displays status and keeps history');

  const reader=app();let request;
  reader.api=async(url,options)=>{request={url,options};return {book_status:'reading'};};
  await reader.startReadingPlanned({id:3,book_id:1,available:true,title:'Book'});
  assert.equal(request.url,'/api/recommendations/3/start');
  assert.equal(reader.shelf,'reading');
  request=null;
  await reader.startReadingPlanned({id:3,book_id:null,available:false});
  assert.equal(request,null);
  console.log('PASS read action uses guarded endpoint and unavailable items are inactive');

  reader.selectedRecommendationRunId=5;
  assert.equal(reader.canResumeRecommendation({book_id:1,book_status:'read'}),true);
  assert.equal(reader.canResumeRecommendation({book_id:1,book_status:'dropped'}),true);
  assert.equal(reader.canResumeRecommendation({book_id:null,book_status:null}),false);
  reader.selectedRecommendationRunId=null;
  assert.equal(reader.canResumeRecommendation({book_id:1,book_status:'read'}),false);
  reader.selectedRecommendationRunId=5;
  await reader.changeRecommendedStatus({id:3,title:'Book'},'resume');
  assert.equal(request.url,'/api/recommendations/3/resume');
  console.log('PASS reread/resume actions are limited to archived existing books');

  for(const handler of ['startReadingPlanned(rec)','addRecommended(rec)']) {
    assert.ok(html.includes(`@click.stop="${handler}" :disabled="!rec.available"`));
  }
  assert.ok(html.includes("changeRecommendedStatus(rec, 'plan')"));
  assert.ok(html.includes("changeRecommendedStatus(rec, 'resume')"));
  console.log('PASS recommendation templates wire availability and both archive actions');

  const dropped=app();let body;
  dropped.patchBook=async(id,b)=>{body=b;};
  dropped.openDetail({id:5,status:'read',drop_reason:'mood',drop_note:'зимой'});
  assert.equal(dropped.detailDropReason,'mood');assert.equal(dropped.detailDropNote,'зимой');
  await dropped.saveDetailStatus();
  assert.deepEqual(plain(body),{status:'read'});
  dropped.detailStatus='dropped';dropped.detailDropReason='lost_thread';dropped.detailDropNote='  перерыв ';
  await dropped.saveDetailStatus();
  assert.deepEqual(plain(body),{status:'dropped',drop_reason:'lost_thread',drop_note:'перерыв'});
  assert.equal(dropped.detail.drop_reason,'lost_thread');
  dropped.detailDropReason='';dropped.detailDropNote='';
  await dropped.saveDetailStatus();
  assert.deepEqual(plain(body),{status:'dropped',drop_reason:null,drop_note:null});
  assert.equal(dropped.dropReasonLabel('slow'),'Затянуто — не устроил темп');
  assert.equal(dropped.dropReasonLabel(null),'Причина не указана');
  console.log('PASS drop reason is sent only with dropped status and can be cleared');

  const titles=app();
  assert.equal(titles.displayTitle({title:'The Martian',title_ru:'Марсианин'}),'Марсианин');
  assert.equal(titles.originalTitle({title:'The Martian',title_ru:'Марсианин'}),'The Martian');
  assert.equal(titles.displayTitle({title:'Пыль',title_ru:null}),'Пыль');
  assert.equal(titles.originalTitle({title:'Пыль',title_ru:'пыль'}),'');
  assert.equal(titles.displayTitle(null),'');
  console.log('PASS Russian title is shown first, original only when it differs');

  for (const aiFirst of [false, true]) {
    const found=app();found.reader={ai:true};found.query='марсианин';
    const ai=[{source_id:'/works/A',title:'The Martian',title_ru:'Марсианин',hint:'The Martian — Andy Weir'}];
    const ol=[{source_id:'/works/A',title:'The Martian'},{source_id:'/works/B',title:'Other'}];
    let releaseOl;const olDone=new Promise(r=>releaseOl=r);
    found.api=async url=>url.includes('/api/search/ai')?ai:(await olDone, ol);
    const searching=found.search();
    assert.equal(found._aiPending,false);
    if (aiFirst) { await found.aiSearch('марсианин',found._searchSeq); releaseOl(); await searching; }
    else { releaseOl(); await searching; await found.aiSearch('марсианин',found._searchSeq); }
    assert.deepEqual(plain(found.results.map(r=>[r.source_id,r.title_ru||null])),[['/works/A','Марсианин'],['/works/B',null]]);
  }
  const short=app();short.reader={ai:true};short.query='ab';short.api=async()=>[];
  await short.search();assert.equal(short._aiPending,false);
  console.log('PASS AI search runs alongside OpenLibrary and its Russian titles win in either order');

  const adding=app();let added;
  adding.api=async(url,opts)=>{added=JSON.parse(opts.body);return {status:'ok',id:7};};
  adding.selected={source_id:'/works/A',title:'The Martian',title_ru:'Марсианин'};adding.addStatus='want';
  await adding.addSelected();
  assert.deepEqual(plain(added),{source_id:'/works/A',status:'want',title:'The Martian',title_ru:'Марсианин'});
  assert.match(adding.toasts.at(-1).message,/«Марсианин» добавлена/);
  console.log('PASS adding from AI search sends the Russian title');

  const editing=app();let patched;
  editing.patchBook=async(id,body)=>{patched=body;};
  editing.editBook({id:3,title:'Dust',title_ru:'Пыль',title_ru_source:'ai',authors:'Hugh Howey',summary:'',description:null,year:null,language:null});
  editing.form.title_ru='Пыль. Укрытие 3';
  await editing.saveBook();
  assert.deepEqual(plain(patched),{title_ru:'Пыль. Укрытие 3'});
  editing.editBook({id:3,title:'Dust',title_ru:'Пыль',authors:'Hugh Howey',summary:'',description:null,year:null,language:null});
  editing.form.title='Dust (Silo 3)';patched=null;
  await editing.saveBook();
  assert.deepEqual(plain(patched),{title:'Dust (Silo 3)'});
  console.log('PASS edit form sends the Russian title only when it changed');

  const x3=app();const hits=[];
  x3.library=[{id:1,title:'Slow Horses',title_ru:'Хромые кони',reader:[{key:'K1'}]}];
  x3.reader={enabled:true,books:44,epubs:6,ok:true,x3:{enabled:true,online:false,listing_checked_at:null,queue:[{key:'K1',filename:'mik.epub',status:'queued'}]}};
  assert.equal(x3.x3BookTitle('K1'),'Хромые кони');assert.equal(x3.x3BookTitle('ZZ'),'ZZ');
  assert.equal(x3.x3Queue().length,1);assert.match(x3.x3StateLine(),/не в сети.*по кнопке/);
  x3.loadDetailReader=async()=>{};x3.loadReaderStatus=async()=>{};
  x3.api=async(url,opts)=>{hits.push([url,opts?.method]);
    if(url==='/api/x3/check') return {enabled:true,online:true,queue:[{key:'K1',filename:'mik.epub'}]};
    return {status:'sent',filename:'mik.epub',online:true};};
  await x3.checkX3();
  assert.equal(x3.reader.x3.online,true);assert.match(x3.toasts.at(-1).message,/X3 в сети — выбери книгу/);
  await x3.sendToX3('K1');
  assert.deepEqual(hits,[['/api/x3/check','POST'],['/api/reader/K1/x3','POST']]);
  assert.match(x3.toasts.at(-1).message,/На X3: mik\.epub/);
  // The server either sends the file or answers with an HTTP error; there is no queued outcome.
  x3.api=async()=>{throw new Error('X3 не в сети. Открой передачу файлов и нажми «Отправить» ещё раз.');};
  await x3.sendToX3('K1');assert.match(x3.toasts.at(-1).message,/^Не получилось: X3 не в сети/);assert.equal(x3.readerBusy,false);
  console.log('PASS X3 queue, check button and manual send');

  const tabbed=app();let libraryLoads=0, x3Online=true;const requests=[];
  tabbed.loadLibrary=async()=>{libraryLoads++;};
  let status={synced_at:'s1',library_revision:'r1',x3:{listing_checked_at:null,history:[]}};
  tabbed.api=async url=>{requests.push(url);
    if(url==='/api/x3/files') return {dir:'/',files:[{name:'a.epub',size:1048576,epub:true,key:'A'}]};
    if(url==='/api/x3/device') return {online:x3Online,status:{version:'1.6.5'}};
    return status;};
  await tabbed.loadReaderStatus();const afterFirst=libraryLoads;
  await tabbed.loadReaderStatus();assert.equal(libraryLoads,afterFirst);
  // The server has just listed the device: the page takes the new check time from the status and reloads the cards.
  status={...status,x3:{listing_checked_at:'2026-10-04T12:00:00Z',history:[]}};
  await tabbed.loadX3Files();
  assert.equal(tabbed.x3Files.length,1);assert.equal(tabbed.x3FilesError,'');
  assert.deepEqual(requests.slice(-2),['/api/x3/files','/api/reader/status']);
  assert.equal(libraryLoads,afterFirst+1);assert.equal(tabbed.reader.x3.listing_checked_at,'2026-10-04T12:00:00Z');
  await tabbed.loadX3Device();
  assert.deepEqual(requests.slice(-2),['/api/x3/device','/api/reader/status']);assert.equal(libraryLoads,afterFirst+2);
  x3Online=false;await tabbed.loadX3Device();assert.equal(libraryLoads,afterFirst+2);
  // A delivery is visible through library_revision; the history feed alone is not a reason to reload.
  status={...status,x3:{...status.x3,history:[{key:'B',sent_at:'t2'}]}};
  await tabbed.loadReaderStatus();assert.equal(libraryLoads,afterFirst+2);
  status={...status,library_revision:'r2'};
  await tabbed.loadReaderStatus();assert.equal(libraryLoads,afterFirst+3);
  tabbed.api=async()=>{throw new Error('X3 не в сети — открой на нём передачу файлов.');};
  await tabbed.loadX3Files();assert.equal(tabbed.x3Files.length,0);assert.match(tabbed.x3FilesError,/не в сети/);
  assert.equal(libraryLoads,afterFirst+3);
  console.log('PASS a fresh X3 listing reloads the cards and the status; otherwise only a new library revision does');

  const dev=app();
  assert.equal(dev.x3Signal(-64),'хороший');assert.equal(dev.x3Signal(-80),'слабый');assert.equal(dev.x3Signal(0),'');
  assert.equal(dev.x3Uptime(3725),'1 ч 2 мин');assert.equal(dev.x3Uptime(251),'4 мин');
  const latest={tag:'1.6.5',published_at:'2026-09-27T09:22:02Z',url:'https://x'};
  assert.equal(dev.x3VersionNote('1.6.5',latest),'последняя');assert.equal(dev.x3VersionNote('1.6.5+abc',latest),'последняя');
  assert.match(dev.x3VersionNote('1.6.5-rc+0f01106',latest),/^вышла 1\.6\.5 · /);
  dev.x3Device={online:false,last_online:'2026-09-30T00:05:01Z',last_sync:1790717280,books:null,latest,
                status:{version:'1.6.5-rc+0f01106',device:'X3',serial:'S1',rssi:-64,freeHeap:54088,uptime:251,ip:'192.168.1.17',mode:'STA'}};
  const fields=Object.fromEntries(dev.x3DeviceFields().map(f=>[f.label,f]));
  assert.equal(fields['Устройство'].value,'X3 · S1');assert.equal(fields['Прошивка'].link,'https://x');
  assert.equal(fields['Wi-Fi'].value,'-64 dBm · хороший');assert.equal(fields['Свободная память'].value,'53 КБ');
  assert.equal(fields['Работает'].value,'—');assert.notEqual(fields['Был в сети'].value,'сейчас');
  assert.equal(fields['Список файлов проверен'].value,'—');
  dev.reader={x3:{enabled:true,listing_checked_at:'2026-10-04T12:00:00Z'}};
  assert.match(dev.x3DeviceFields().find(f=>f.label==='Список файлов проверен').value,/^04\.10\.2026 \d\d:\d\d$/);
  console.log('PASS X3 device card formats status, firmware note, offline data and the time of the last file listing');

  const gen=app();const reqs=[];
  gen.loadDetailReader=async()=>{};gen.loadReaderStatus=async()=>{};gen.pollGeneratedEpub=()=>{};
  gen.api=async(url,opts)=>{reqs.push([url,opts?.method]);return {status:'generating'};};
  await gen.generateEpub('K1');
  await gen.forgetDocument('55f405a5');
  assert.deepEqual(reqs,[['/api/reader/K1/generate-epub','POST'],['/api/reader/documents/55f405a5','DELETE']]);
  assert.equal(gen.statusText('generating'),'собирается из KFX');assert.equal(gen.indicatorState('generating'),'pending');
  console.log('PASS generate EPUB button and removing an unbound X3 request');

  const manual=context.app();const aiRequests=[];
  manual.api=async url=>{aiRequests.push(url);return {stale:true,planned:[],external:[],history:[],pending:false};};
  manual.loadRecommendationHistory=async()=>{};
  manual.refreshRecommendations=async()=>{throw new Error('Automatic recommendations are forbidden');};
  await manual.loadRecommendations();
  assert.ok(aiRequests.every(url=>!url.includes('/refresh')));
  manual.query='some book';manual.reader={ai:true};manual.api=async url=>{aiRequests.push(url);return [];};
  await manual.search();
  assert.ok(aiRequests.every(url=>!url.includes('/search/ai')));
  await manual.manualAiSearch();
  assert.ok(aiRequests.some(url=>url.includes('/search/ai')));
  assert.equal(manual.bookPercent({epubs:[{canonical:1,percentage:.35}],reader:[]}),35);
  console.log('PASS stale recommendations and ordinary search never call AI; EPUB-only progress is visible');

  const pct=app();
  assert.equal(pct.fmtPct(19.736),'19.7%');assert.equal(pct.fmtPct(14.47),'14.5%');assert.equal(pct.fmtPct(14.02),'14%');
  assert.equal(pct.fmtPct(0.54),'0.5%');assert.equal(pct.fmtPct(100),'100%');assert.equal(pct.fmtPct(null),'0%');
  const book={reader:[{percentage:0.19736842,present:1}]};
  assert.equal(pct.bookPercentText(book),pct.fmtPct(pct.readerPct(book.reader[0])));  // карточка = страница книги
  let loads=0;pct.loadLibrary=async()=>{loads++;};
  let st={synced_at:'s',snapshots_at:'a'};pct.api=async()=>st;
  await pct.loadReaderStatus();const base=loads;
  await pct.loadReaderStatus();assert.equal(loads,base);
  st={synced_at:'s',snapshots_at:'b'};await pct.loadReaderStatus();assert.equal(loads,base+1);
  console.log('PASS one percent format everywhere and cards reload when reader data changes');

  const files=app();
  const koBook={id:20,status:'unsorted',epubs:[{sha256:'a'.repeat(64),canonical:1}],reader:[],devices:[{device:'KOReader',device_id:'one'}]};
  const metadataBook={id:21,epubs:[],reader:[],devices:[]};
  assert.equal(files.matchesFile(koBook,'reader'),true);
  assert.equal(files.matchesFile(koBook,'none'),false);
  assert.equal(files.matchesFile(koBook,'epub'),true);
  assert.equal(files.matchesFile(koBook,'no_epub'),false);
  assert.equal(files.matchesFile(metadataBook,'no_epub'),true);
  assert.deepEqual(plain(files.bookDeviceNames(koBook)),['KOReader']);
  assert.equal(files.indicatorState('unresolved'),'pending');
  console.log('PASS file filters use EPUB assets and generic device observations, including catalogue-only cards');

  const binding=app(); let bound;
  binding.library=[{...koBook,reader:[{key:'KINDLE_20',filename:'book.kfx'}]}];
  assert.equal(binding.bindTargets()[0].key,'book:20:'+'a'.repeat(64));
  assert.equal(binding.kindleBindTargets()[0].key,'KINDLE_20');
  binding.incomingChoice[4]=binding.kindleBindTargets()[0].key;
  binding.api=async (url,opts)=>{bound={url,body:JSON.parse(opts.body)}; return {attached:['KINDLE_20']};};
  binding.loadReaderStatus=async()=>{};
  await binding.attachIncoming(4);
  assert.deepEqual(plain(bound),{url:'/api/incoming/4/attach',body:{key:'KINDLE_20'}});
  await binding.bindDocument('unknown',binding.bindTargets()[0].key);
  assert.deepEqual(plain(bound.body),{book_id:20,sha256:'a'.repeat(64)});
  console.log('PASS incoming EPUB and KOSync binding send the identifier expected by their own endpoints');

  const uploaded=app();uploaded.library=[koBook];uploaded.addOpen=true;
  uploaded.shelf='reading';uploaded.fileFilter='no_epub';uploaded.libraryQuery='other';
  uploaded.api=async()=>({book_id:20});uploaded.loadDetailReader=async()=>{};
  const event={target:{files:[{name:'new.epub'}],value:'new.epub'}};
  await uploaded.uploadLibraryEpub(event);
  assert.equal(uploaded.detail.id,20);assert.equal(uploaded.detailOpen,true);assert.equal(uploaded.addOpen,false);
  assert.equal(uploaded.shelf,'unsorted');assert.equal(uploaded.libraryQuery,'');assert.equal(uploaded.fileFilter,'all');
  assert.equal(uploaded.readerBusy,false);assert.equal(event.target.value,'');
  console.log('PASS uploading from a filtered shelf reveals and opens the resulting book');

  const refreshed=context.app();refreshed.detail={id:20,epubs:[{percentage:.1}]};refreshed.detailOpen=true;
  refreshed.detailStatus='dropped';refreshed.detailDropNote='unsaved note';
  refreshed.api=async()=>[{...koBook,epubs:[{canonical:1,percentage:.7}]}];
  await refreshed.loadLibrary();
  assert.equal(refreshed.detail.epubs[0].percentage,.7);assert.equal(refreshed.detailStatus,'dropped');assert.equal(refreshed.detailDropNote,'unsaved note');
  refreshed.api=async()=>{throw Error('offline');};await refreshed.loadLibrary();
  assert.match(refreshed.libraryError,/offline/);assert.equal(refreshed.library.length,1);assert.equal(refreshed.libraryLoading,false);
  console.log('PASS live progress updates the open card without overwriting a status draft; refresh failures retain books');

  const seen=app();seen.devices={kindle:[{id:'k',last_seen:10,books_count:2}],readers:[
    {device_id:'a',device:'KOReader',received_at:12},{device_id:'a',device:'KOReader',received_at:8},
    {device_id:'b',device:'KOReader',received_at:11}]};
  assert.deepEqual(plain(seen.deviceCards().map(d=>[d.id,d.seen])),[['a',12],['b',11],['k',10]]);
  seen.detail={epubs:[{canonical:1,sha256:'a'},{canonical:0,sha256:'b'}]};
  assert.equal(seen.visibleEpubs().length,1);seen.showOtherEpubs=true;assert.equal(seen.visibleEpubs().length,2);
  console.log('PASS two KOReaders stay distinct, repeated observations collapse, secondary EPUB versions are opt-in');

  // The server decides X3 presence (book.x3); the page only words it. X3 being configured adds nothing.
  const presence=app();presence.reader={x3:{enabled:true,history:[]}};
  const listed={state:'present',confirmed:true,checked_at:'2026-10-04T12:00:00Z',sent_at:'2026-10-01T09:00:00Z',synced_at:null,filename:'sent.epub'};
  const sentOnly={...listed,confirmed:false,checked_at:'2026-09-20T08:00:00Z',synced_at:'2026-10-03T10:00:00Z'};
  const gone={...listed,state:'missing',confirmed:false};
  let badge=presence.x3BookBadge({id:7,x3:listed});
  assert.deepEqual([badge.label,badge.state],['X3: есть','ready']);
  assert.match(badge.title,/^Файл найден на X3 при проверке 04\.10\.2026 \d\d:\d\d$/);
  badge=presence.x3BookBadge({id:7,x3:sentOnly});
  assert.deepEqual([badge.label,badge.state],['X3: есть','ready']);
  assert.equal(badge.title,'Отправлено или синхронизировано 03.10.2026; с тех пор X3 не проверялась');
  assert.match(presence.x3BookBadge({id:7,x3:{...sentOnly,synced_at:null}}).title,/синхронизировано 01\.10\.2026;/);
  badge=presence.x3BookBadge({id:7,x3:gone});
  assert.deepEqual([badge.label,badge.state],['X3: нет','pending']);
  assert.match(badge.title,/^При проверке 04\.10\.2026 \d\d:\d\d файла на X3 не оказалось$/);
  assert.equal(presence.x3BookBadge({id:8,x3:null}),null);assert.equal(presence.x3BookBadge({id:8}),null);assert.equal(presence.x3BookBadge(null),null);
  assert.equal(presence.matchesFile({id:7,reader:[],devices:[],x3:sentOnly},'reader'),true);
  assert.equal(presence.matchesFile({id:8,reader:[],devices:[],x3:null},'none'),true);
  assert.doesNotMatch(html,/X3: (нет данных|отправлено|был синк|не найдено)|x3FilesCheckedAt|isX3Name|x3\??\.interval/);
  const x3Chip=html.match(/<span[^>]*>(?=<\/span>)/g).find(tag=>tag.includes("'На X3 · '"))||'';
  assert.ok(x3Chip.includes('x-show="x3BookBadge(detail)"')&&x3Chip.includes("detail?.x3?.state === 'present' ? 'есть' : 'нет'"));
  console.log('PASS X3 badge words the server verdict: confirmed, sent or synced since the last listing, missing, and nothing without evidence');

  const crossPoint={device:'CrossPoint',device_id:'x',x3:true}, koReader={device:'KOReader',device_id:'k',x3:false};
  const shelved={id:7,epubs:[{canonical:1,percentage:0}],reader:[{present:true,percentage:0}],devices:[crossPoint,koReader],x3:listed};
  const indicators=presence.bookIndicators(shelved);
  assert.deepEqual(plain(indicators.map(i=>i.label)),['Kindle: есть','X3: есть','KOReader: был синк','EPUB: есть']);
  assert.equal(indicators.find(i=>i.key==='x3').state,'ready');
  // An X3 observation is never a device chip of its own, even when the server reports no X3 state (read or dropped book).
  assert.deepEqual(plain(presence.bookIndicators({...shelved,devices:[crossPoint],x3:null}).map(i=>i.label)),['Kindle: есть','EPUB: есть']);
  assert.equal(presence.matchesFile({id:9,reader:[],devices:[crossPoint],x3:null},'reader'),true);
  assert.equal(presence.hasBookProgress({epubs:[{canonical:1,percentage:0}]}),true);
  assert.equal(presence.hasBookProgress({epubs:[],reader:[]}),false);
  assert.equal(presence.bookIndicators({reader:[{present:false}],epubs:[]})[0].label,'Kindle: удалена');
  console.log('PASS shared book indicators show X3 once, keep other readers, removed Kindle state and a real zero-percent position');
  assert.deepEqual(plain(presence.coverIndicators(shelved).map(i=>i.shortLabel)),['Kindle','X3','KOReader ↔','EPUB']);
  const compact=presence.coverIndicators({...shelved,devices:[],x3:gone});
  assert.deepEqual(plain(compact.map(i=>i.shortLabel)),['Kindle','X3 −','EPUB']);
  assert.equal(compact[1].label,'X3: нет');
  assert.deepEqual(plain(presence.coverIndicators({epubs:[]}).map(i=>i.shortLabel)),['EPUB −']);
  console.log('PASS compact cover indicators retain full accessible state labels');

  const translation=app();let translationCalls=0, polledId=null, finishTranslationRequest;
  translation.reader.ai=true;translation.detailOpen=true;translation.detail={id:41,description:'Original'};
  translation.startDescriptionPoll=id=>{polledId=id;};
  translation.api=async(url,options)=>{
    assert.equal(url,'/api/books/41/translate-description');assert.equal(options.method,'POST');
    translationCalls++;return new Promise(resolve=>{finishTranslationRequest=resolve;});
  };
  const translating=translation.translateDescription();
  await translation.translateDescription();assert.equal(translationCalls,1);
  finishTranslationRequest({status:'queued'});await translating;
  assert.equal(translation.detail.description,'Original');assert.equal(translation.detail.description_translation_status,'pending');
  assert.equal(polledId,41);await translation.translateDescription();assert.equal(translationCalls,1);
  translation.detail={id:41,description:'Original'};polledId=null;
  const delayed=translation.translateDescription();
  translation.detail={id:42,description:'Another book'};finishTranslationRequest({status:'queued'});await delayed;
  assert.equal(polledId,null);assert.equal(translation.detail.description_translation_status,undefined);
  console.log('PASS translation is explicit, deduplicates clicks and cannot change another open book');

  let pendingPoll;
  const pollingContext={...context,setTimeout:fn=>{pendingPoll=fn;return 1;}};
  vm.createContext(pollingContext);vm.runInContext(script,pollingContext);
  const polling=pollingContext.app();polling.detailOpen=true;polling.detail={id:41,description:'Original',summary:'Review'};
  polling.detailStatus='dropped';polling.library=[polling.detail];
  polling.api=async()=>({id:41,description:'Original',description_ru:'Перевод',description_translation_status:'ready',status:'reading'});
  polling.startDescriptionPoll(41);await pendingPoll();
  assert.equal(polling.detail.description_ru,'Перевод');assert.equal(polling.detailStatus,'dropped');assert.equal(polling.detail.summary,'Review');
  let finishPoll;polling.api=()=>new Promise(resolve=>{finishPoll=resolve;});
  polling.startDescriptionPoll(41);const oldPoll=pendingPoll();polling.stopDescriptionPoll();polling.detail={id:42,description:'Other'};
  finishPoll({id:41,description_ru:'Late response'});await oldPoll;assert.equal(polling.detail.description_ru,undefined);
  console.log('PASS translation polling preserves status drafts and ignores responses after navigation');

  const delivery=app();let refreshedAfterSend=0;
  delivery.detail={id:7};
  delivery.api=async()=>({status:'sent',filename:'sent.epub'});
  delivery.loadLibrary=async()=>{refreshedAfterSend++;};delivery.loadReaderStatus=async()=>{};
  await delivery.sendLibraryEpub({sha256:'a'.repeat(64)});
  assert.equal(refreshedAfterSend,1);
  const firstSend=app();let firstRefresh=0;firstSend.loadLibrary=async()=>{firstRefresh++;};
  firstSend.api=async()=>({synced_at:'same',library_revision:1,x3:{history:[]}});
  await firstSend.loadReaderStatus();const beforeSend=firstRefresh;
  firstSend.api=async()=>({synced_at:'same',library_revision:2,x3:{history:[{key:'book:7',sent_at:'now'}]}});
  await firstSend.loadReaderStatus();assert.equal(firstRefresh,beforeSend+1);
  console.log('PASS successful delivery and the first background delivery refresh card evidence immediately');

  const listView='#tab=library&shelf=unsorted&file=epub&q=book';
  const {ui:routed,page}=routedApp(listView,[koBook]);const location=page.window.location;
  await routed.init();assert.equal(routed.shelf,'unsorted');assert.equal(routed.libraryQuery,'book');
  routed.openDetail(koBook);assert.match(location.hash,/book=20/);
  location.hash=listView;await routed.readRoute();assert.equal(routed.detailOpen,false);
  location.hash='#book=20';await routed.readRoute();assert.equal(routed.detail.id,20);assert.equal(routed.detailOpen,true);
  const entries=page.window.history.length;
  routed.closeDetail();assert.doesNotMatch(location.hash,/book=/);
  assert.equal(page.queued,0);assert.equal(page.window.history.length,entries);
  console.log('PASS book links, shelf/search restoration, and a card opened from a direct link closes in place');

  const secondBook={...koBook,id:21};
  const {ui:shelfUi,page:tab}=routedApp(listView,[koBook,secondBook]);const tabHistory=tab.window.history;
  await shelfUi.init();assert.equal(tabHistory.length,1);
  for (let cycle=0;cycle<5;cycle++) {
    shelfUi.openDetail(cycle%2?secondBook:koBook);
    assert.equal(tabHistory.state.card,true);assert.match(tab.window.location.hash,/book=2[01]$/);
    shelfUi.closeDetail();assert.equal(shelfUi.detailOpen,false);
    await tab.traverse();
    assert.equal(tab.window.location.hash,listView);assert.equal(shelfUi.detailOpen,false);
  }
  assert.equal(tabHistory.length,2);assert.equal(tab.index,0);
  console.log('PASS five viewed cards leave one forward entry instead of five duplicates of the list');

  shelfUi.openDetail(koBook);shelfUi.closeDetail();shelfUi.closeDetail();
  assert.equal(tab.queued,-1);await tab.traverse();assert.equal(tab.window.location.hash,listView);
  tabHistory.forward();await tab.traverse();assert.equal(shelfUi.detail.id,20);assert.equal(shelfUi.detailOpen,true);
  shelfUi.openDetail(secondBook);
  assert.equal(tabHistory.length,2);assert.equal(tabHistory.state.card,true);assert.match(tab.window.location.hash,/book=21$/);
  shelfUi.editBook(shelfUi.detail);shelfUi.addOpen=true;shelfUi.recommendationDetailOpen=true;
  tabHistory.back();await tab.traverse();
  assert.deepEqual([shelfUi.detailOpen,shelfUi.editing,shelfUi.addOpen,shelfUi.recommendationDetailOpen],[false,false,false,false]);
  shelfUi.openDetail(koBook);shelfUi.navigate('devices');
  assert.equal(shelfUi.detailOpen,false);assert.equal(tabHistory.length,3);assert.equal(tabHistory.state.card,false);
  tabHistory.back();await tab.traverse();assert.equal(shelfUi.detailOpen,true);assert.equal(shelfUi.tab,'library');
  shelfUi.closeDetail();await tab.traverse();assert.equal(tab.index,0);assert.equal(tab.window.location.hash,listView);
  console.log('PASS repeated close goes back once, a second card reuses the entry, and Back closes every stacked dialog');

  shelfUi.openDetail(secondBook);shelfUi.closeDetail();await tab.traverse();
  shelfUi.library=shelfUi.library.filter(book=>book.id!==21);
  tabHistory.forward();await tab.traverse();
  assert.equal(shelfUi.detailOpen,false);assert.match(shelfUi.toasts.at(-1).message,/не найдена/);
  assert.doesNotMatch(tab.window.location.hash,/book=/);assert.equal(tabHistory.state.card,false);
  shelfUi.library=[koBook,secondBook];
  shelfUi.openDetail(koBook);shelfUi.editBook(shelfUi.detail);shelfUi.form.title='Черновик';
  tabHistory.back();await tab.traverse();
  assert.deepEqual([shelfUi.detailOpen,shelfUi.editing,shelfUi.form.title],[false,true,'Черновик']);
  shelfUi.editing=false;
  console.log('PASS a history entry of a vanished book is rewritten, and Back keeps an edit form with unsaved changes');

  const {ui:late,page:lateTab}=routedApp('#tab=library&shelf=all&file=all&q=&book=20',[koBook]);
  const books=late.api;let failures=0;
  late.api=async url=>{if(url==='/api/books'){failures++;throw Error('offline');}return books(url);};
  await late.init();
  assert.equal(failures,1);assert.match(late.libraryError,/offline/);assert.equal(late.detailOpen,false);
  assert.match(lateTab.window.location.hash,/book=20$/);assert.equal(late.toasts.length,0);
  late.shelf='reading';late.writeRoute();assert.match(lateTab.window.location.hash,/shelf=reading.*book=20$/);
  late.api=books;await late.loadLibrary();
  assert.equal(late.detailOpen,true);assert.equal(late.detail.id,20);assert.match(lateTab.window.location.hash,/book=20$/);
  late.closeDetail();assert.equal(lateTab.queued,0);assert.doesNotMatch(lateTab.window.location.hash,/book=/);
  await late.loadLibrary();assert.equal(late.detailOpen,false);
  const {ui:stale,page:staleTab}=routedApp('#tab=library&shelf=all&file=all&q=&book=99',[koBook]);
  stale.api=async()=>{throw Error('offline');};await stale.init();
  stale.api=books;await stale.loadLibrary();
  assert.equal(stale.detailOpen,false);assert.match(stale.toasts.at(-1).message,/не найдена/);assert.doesNotMatch(staleTab.window.location.hash,/book=/);
  console.log('PASS a book link survives a failed first load and opens once the library arrives');

  const resetButton=(html.match(/Перестроить карту<\/button>\s*(<button[^>]*>Сбросить позицию Kindle…<\/button>)/)||['',''])[1];
  assert.ok(resetButton.includes(`@click="readerAction(it.key, 'reset')"`)&&resetButton.includes('x-show="it.pid != null || it.percentage != null"'));
  const reset=app();const resetCalls=[];let question='';
  reset.api=async(url,options)=>{resetCalls.push([url,options.method]);return {status:'reset'};};reset.loadDetailReader=async()=>{};
  context.confirm=text=>{question=text;return false;};
  await reset.readerAction('K1','reset');assert.deepEqual(resetCalls,[]);assert.match(question,/^Сбросить позицию\?/);
  context.confirm=()=>true;
  await reset.readerAction('K1','reset');assert.deepEqual(resetCalls,[['/api/reader/K1/reset','POST']]);
  assert.match(reset.toasts.at(-1).message,/Позиция сброшена/);assert.equal(reset.readerBusy,false);
  console.log('PASS a Kindle position can be reset without an EPUB position, after the existing confirmation');

  const vendored=[...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"[^>]*>/g)];
  assert.deepEqual(vendored.map(tag=>tag[1].replace(/\d+(?:\.\d+)+/,'N')),['/static/vendor/tailwind-N.js','/static/vendor/alpine-N.min.js']);
  assert.deepEqual(vendored.map(tag=>/\sdefer\b/.test(tag[0])),[false,true]);
  assert.deepEqual([...html.matchAll(/<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']?\s*((?:https?:)?\/\/[^"'\s>]+)/gi)].map(tag=>tag[1]),[]);
  for (const style of html.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/g)) assert.doesNotMatch(style[1],/@import|url\(\s*["']?(?:https?:)?\/\//);
  for (const tag of vendored) {
    const source=fs.readFileSync(path.join(__dirname,'..',tag[1]),'utf8');
    assert.ok(source.length>50000);
    assert.doesNotMatch(source,/\bfetch\(|XMLHttpRequest|sendBeacon|importScripts|WebSocket|EventSource|createElement\(["'](?:script|link)["']\)/);
  }
  console.log('PASS the page loads only vendored scripts, which make no network requests of their own');

  const health=app();
  const line=reader=>{health.reader=reader;return plain(health.serviceHealth());};
  assert.deepEqual(line({ok:true,services:{sync:true,worker:true}}),{ok:true,text:'Синхронизация работает'});
  assert.deepEqual(line({ok:true,services:{sync:false,worker:true}}),{ok:false,text:'Сервис синхронизации не отвечает'});
  assert.deepEqual(line({ok:true,services:{sync:true,worker:false}}),{ok:false,text:'Фоновый обработчик не отвечает'});
  assert.deepEqual(line({ok:false,services:{sync:false,worker:false}}),{ok:false,text:'Сервис синхронизации не отвечает · Фоновый обработчик не отвечает'});
  assert.deepEqual(line({ok:false,error:'boom',services:{sync:true,worker:true}}),{ok:false,text:'Данные читалок недоступны: boom'});
  assert.deepEqual(line({ok:true}),{ok:true,text:'Синхронизация работает'});
  assert.deepEqual(line({ok:false}),{ok:false,text:'Сервис синхронизации не отвечает'});
  // A stale "all fine" must not outlive a status request that failed.
  health.reader={ok:true,services:{sync:true,worker:true}};health.api=async()=>{throw Error('Failed to fetch');};
  await health.loadReaderStatus();assert.deepEqual(plain(health.serviceHealth()),{ok:false,text:'Нет связи с сервером'});
  health.api=async()=>({ok:true,services:{sync:true,worker:true}});
  await health.loadReaderStatus();assert.equal(health.serviceHealth().ok,true);
  assert.ok(html.includes('x-text="serviceHealth().text"'));assert.doesNotMatch(html,/Сервис синхронизации работает/);
  const waiting=app();const removed=[];
  waiting.api=async(url,options)=>{removed.push([url,options?.method]);return {status:'deleted'};};waiting.loadReaderStatus=async()=>{};
  context.confirm=text=>{question=text;return true;};
  await waiting.deleteIncoming({id:4,filename:'book.epub'});
  context.confirm=()=>true;
  assert.deepEqual(removed,[['/api/incoming/4','DELETE']]);
  assert.match(question,/«book\.epub»/);assert.match(question,/EPUB останется в библиотеке/);assert.doesNotMatch(question,/удалится из BookHub/);
  console.log('PASS the status line names the process that is down, and removing a waiting EPUB says the file stays');

  // deleted_before comes from the server with the book and with the answer to an add; the page only words it.
  const note={id:3,deleted_at:'2026-10-03T12:00:00Z',title:'Dune'}, noteText='Эта книга уже удалялась 03.10.2026';
  const again=app();const forgotten=[];
  again.library=[{id:30,title:'Dune',status:'want',deleted_before:note},{id:31,title:'Other',status:'want',deleted_before:null}];
  again.openDetail(again.library[0]);
  assert.equal(again.deletedBeforeText(again.detail.deleted_before),noteText);assert.equal(again.deletedBeforeText(null),'');
  again.api=async()=>{throw new Error('offline');};
  await again.forgetDeleted();
  assert.equal(again.detail.deleted_before.id,3);assert.equal(again.toasts.at(-1).message,'Не удалось убрать заметку: offline');
  again.api=async(url,options)=>{forgotten.push([url,options?.method]);return {status:'forgotten'};};
  await again.forgetDeleted();
  assert.deepEqual(forgotten,[['/api/deleted/3','DELETE']]);
  assert.equal(again.detail.deleted_before,null);assert.equal(again.library[0].deleted_before,null);
  await again.forgetDeleted();assert.equal(forgotten.length,1);
  const noteTag=(html.match(/<p[^>]*x-show="detail\?\.deleted_before"[^>]*>[\s\S]*?<\/p>/)||[''])[0];
  assert.ok(noteTag.includes('x-text="deletedBeforeText(detail?.deleted_before)"'));
  assert.match(noteTag,/<button[^>]*@click="forgetDeleted\(\)"[^>]*>✕<\/button>/);
  const [beforeCard,card]=html.split('x-ref="bookDialog"');
  assert.ok(card.indexOf(noteTag)<card.indexOf('class="bh-book-summary"'));
  assert.doesNotMatch(beforeCard,/deleted_before|deletedBeforeText/);
  console.log('PASS a card deleted before says so near its header, ✕ forgets the note on the server, and the shelf grid has no note');

  const messages=ui=>plain(ui.toasts.map(t=>[t.message,t.kind]));
  const readded=app();
  readded.api=async()=>({status:'ok',id:30,deleted_before:note});
  readded.selected={source_id:'/works/D',title:'Dune'};readded.addStatus='want';
  await readded.addSelected();
  assert.deepEqual(messages(readded),[['«Dune» добавлена','success'],[noteText,'info']]);
  readded.toasts=[];readded.loadRecommendations=async()=>{};
  await readded.addRecommended({id:2,source_id:'/works/D',title:'Dune'});
  assert.deepEqual(messages(readded),[['«Dune» в планах','success'],[noteText,'info']]);
  readded.toasts=[];readded.api=async()=>({status:'ok',id:31,deleted_before:null});
  await readded.addRecommended({id:2,source_id:'/works/O',title:'Other'});
  assert.deepEqual(messages(readded),[['«Other» в планах','success']]);
  const reuploaded=app();reuploaded.library=[koBook];
  reuploaded.api=async()=>({book_id:20,deleted_before:note});
  await reuploaded.uploadLibraryEpub({target:{files:[{name:'dune.epub'}],value:'dune.epub'}});
  assert.deepEqual(messages(reuploaded),[['EPUB сохранён в библиотеке','success'],[noteText,'info']]);
  console.log('PASS adding from the catalogue or uploading an EPUB that was deleted before says so in a toast');

  // The page words an HTTP status only where the server answers differ in meaning; it must survive api().
  context.fetch=async()=>({ok:false,status:409,json:async()=>({detail:'На X3 нет файлов этой книги.'})});
  await assert.rejects(context.app().api('/api/books/7/x3-remove',{method:'POST'}),error=>error.status===409&&error.message==='На X3 нет файлов этой книги.');
  delete context.fetch;
  const removal=context.app();const removalCalls=[];let onX3=true, refusal=null;
  removal.api=async(url,options)=>{
    if(url==='/api/books') return [{id:7,title:'Sent',epubs:[],reader:[],devices:[],x3:onX3?listed:null}];
    if(url==='/api/reader/status') return {synced_at:'s',library_revision:1};
    removalCalls.push([url,options?.method]);
    if(refusal) throw Object.assign(new Error(refusal[1]),{status:refusal[0]});
    onX3=false;return {status:'removed',files:['sent.epub'],contact:{positions:0,removed:[]}};
  };
  await removal.loadLibrary();removal.openDetail(removal.library[0]);
  context.confirm=text=>{question=text;return false;};
  await removal.removeFromX3();
  assert.deepEqual(removalCalls,[]);assert.equal(question,'Удалить файл этой книги с X3? На полке книга и EPUB останутся.');
  context.confirm=()=>true;
  refusal=[503,'X3 не в сети.'];await removal.removeFromX3();
  assert.deepEqual(removalCalls,[['/api/books/7/x3-remove','POST']]);
  assert.equal(removal.toasts.at(-1).message,'X3 не в сети — открой на ней передачу файлов');assert.equal(removal.detail.x3.state,'present');
  refusal=[409,'На X3 нет файлов этой книги.'];await removal.removeFromX3();
  assert.equal(removal.toasts.at(-1).message,'На X3 этого файла уже нет');
  refusal=[502,'X3 не удалила файл'];await removal.removeFromX3();
  assert.equal(removal.toasts.at(-1).message,'Не получилось: X3 не удалила файл');assert.equal(removal.readerBusy,false);
  refusal=null;await removal.removeFromX3();
  assert.equal(removalCalls.length,4);assert.deepEqual(messages(removal).at(-1),['Убрано с X3','success']);
  assert.equal(removal.detail.x3,null);assert.equal(removal.library[0].x3,null);assert.equal(removal.readerBusy,false);
  const removeButton=(html.match(/<button[^>]*>Убрать с X3…<\/button>/)||[''])[0];
  assert.ok(removeButton.includes(`x-show="detail?.x3?.state === 'present'"`)&&removeButton.includes('@click="removeFromX3()"'));
  console.log('PASS «Убрать с X3…» asks first, words an offline X3 and a missing file, and refreshes the card after removal');

  const contacted=app();
  for (const silent of [null,{},{contact:null},{contact:{positions:0,removed:[]}}]) contacted.reportX3Contact(silent);
  assert.equal(contacted.toasts.length,0);
  contacted.reportX3Contact({contact:{positions:2,removed:[]}});
  contacted.reportX3Contact({contact:{positions:0,removed:['old.epub','gone.epub']}});
  assert.deepEqual(messages(contacted),[['С X3 прочитано позиций: 2','info'],['С X3 удалены файлы удалённых книг: old.epub, gone.epub','info']]);
  const told={positions:1,removed:['old.epub']}, report='С X3 прочитано позиций: 1 · С X3 удалены файлы удалённых книг: old.epub';
  for (const [url,call] of [['/api/x3/check',ui=>ui.checkX3()],['/api/x3/device',ui=>ui.loadX3Device()],['/api/x3/files',ui=>ui.loadX3Files()],
      ['/api/reader/K1/x3',ui=>ui.sendToX3('K1')],['/api/books/7/send-x3?sha256=abc',ui=>ui.sendLibraryEpub({sha256:'abc'})],
      ['/api/books/7/x3-remove',ui=>ui.removeFromX3()]]) {
    const caller=app();const asked=[];
    caller.detail={id:7,x3:listed};caller.loadDetailReader=async()=>{};caller.loadReaderStatus=async()=>{};
    caller.api=async requested=>{asked.push(requested);return {online:true,files:[],filename:'sent.epub',contact:told};};
    await call(caller);
    assert.deepEqual(asked,[url]);assert.deepEqual(messages(caller).filter(m=>m[0].startsWith('С X3')),[[report,'info']]);
  }
  // «Проверить X3» on the devices tab also reloads the device card and the file list: three contacts, one report.
  const chained=app();const contacts=[];
  chained.tab='devices';chained.loadReaderStatus=async()=>{};
  chained.api=async url=>{contacts.push(url);return {online:true,files:[],contact:{positions:1,removed:[]}};};
  await chained.checkX3();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(contacts.sort(),['/api/x3/check','/api/x3/device','/api/x3/files']);
  assert.equal(chained.toasts.filter(t=>t.message==='С X3 прочитано позиций: 1').length,1);
  console.log('PASS every call that reaches the X3 reports positions read and files removed in one toast, and stays silent when there are none');

  // The OPDS menu editor against the shelf's side of the contract: GET answers the stored menu, PUT stores the body and
  // gives an id to a new section, DELETE restores the built-in menu. refuse turns the menu's answers into an HTTP error,
  // down fails every request; a GET asked while hold is set is answered when that promise resolves, with the menu as it
  // was when asked.
  const builtinMenu=[
    {id:'reading',title:'Читаю',enabled:true,filter:{status:['reading']},sort:'read',limit:null,count:3,builtin:true},
    {id:'want',title:'В планах',enabled:true,filter:{status:['want']},sort:'added',limit:null,count:12,builtin:true},
    {id:'new',title:'Новые',enabled:true,filter:{added_days:30},sort:'added',limit:30,count:5,builtin:true},
    {id:'all',title:'Все',enabled:true,filter:{},sort:'title',limit:null,count:20,builtin:true},
    {id:'unsorted',title:'Общее',enabled:false,filter:{status:['unsorted']},sort:'added',limit:null,count:5,builtin:true},
    {id:'no-x3',title:'Нет на X3',enabled:false,filter:{on_x3:false},sort:'title',limit:null,count:9,builtin:true}];
  function opdsShelf(enabled=true,path='/opds') {
    const shelf={requests:[],refuse:null,down:false,hold:null,stored:null,agree:true,asked:''};
    shelf.fetch=async(url,options={})=>{
      const method=options.method||'GET', body=options.body?JSON.parse(options.body):undefined;
      shelf.requests.push(body?[method,url,body]:[method,url]);shelf.headers=options.headers;
      if(shelf.down) throw new TypeError('Failed to fetch');
      if(url!=='/api/opds/menu') return {ok:true,status:200,json:async()=>({kindle:[],readers:[]})};
      if(shelf.refuse) return {ok:false,status:shelf.refuse[0],json:async()=>({detail:shelf.refuse[1]})};
      if(method==='PUT') shelf.stored=body;
      if(method==='DELETE') shelf.stored=null;
      const sections=(shelf.stored?.sections||builtinMenu).map((s,n)=>{
        const known=builtinMenu.find(b=>b.id===s.id);
        return {...s,id:s.id||'own'+n,count:known?known.count:2,builtin:!!known};
      });
      const answer=plain({enabled,path,page_size:shelf.stored?.page_size??20,sections,
        filters:{status:['reading','want','unsorted'],sort:['read','added','title','author','updated']},default:!shelf.stored});
      if(method==='GET') await shelf.hold;
      return {ok:true,status:200,json:async()=>answer};
    };
    return shelf;
  }
  function opdsPage(shelf) {
    const tabPage=browser('#tab=library');tabPage.window.location.origin='http://books.waifu';
    const pageContext={...context,URLSearchParams,window:tabPage.window,fetch:shelf.fetch,confirm:text=>{shelf.asked=text;return shelf.agree;}};
    vm.createContext(pageContext);vm.runInContext(script,pageContext);
    return pageContext.app();
  }
  const devicesTab=html.slice(html.indexOf(`<section x-show="tab === 'devices'"`),html.indexOf(`<section x-show="tab === 'recommendations'"`));
  const opdsBlock=(html.match(/<section aria-label="Каталог для читалок \(OPDS\)"[\s\S]*?<\/section>/)||[''])[0];
  const visible=(view,tag,text)=>view.filter(el=>el.shown&&el.tag===tag&&(text===undefined||el.text===text));
  // One line per menu section as the owner sees it: the switch, the title, then the texts of the row; a select shows its chosen option.
  const token=el=>el.tag==='input'?(el.attrs.type==='checkbox'?(el.bound['x-model']?'on':'off'):String(el.bound['x-model']))
    :el.tag==='option'&&(el.bound[':value']??el.attrs.value)!==el.parent.bound['x-model']?'':el.text;
  const menuRows=view=>[...new Set(view.map(el=>el.scope.s).filter(Boolean))].map(s=>view.filter(el=>el.shown&&el.scope.s===s).map(token).filter(Boolean).join(' | '));
  const signIn=view=>visible(view,'p').map(el=>el.text).filter(text=>text.includes('синка'));

  const shelfSide=opdsShelf(), menu=opdsPage(shelfSide), see=()=>render(menu,opdsBlock);
  const control=text=>see().find(el=>el.shown&&el.text===text);
  const field=(model,n)=>see().find(el=>el.shown&&el.attrs['x-model']===model&&el.scope.i===n);
  const arrow=(n,sign)=>see().find(el=>el.scope.i===n&&el.text===sign);
  const unsaved=()=>[!!control('Есть несохранённые изменения'),!control('Сохранить').disabled,!!control('Отменить правки')];
  menu.navigate('devices');
  assert.deepEqual(see().filter(el=>el.shown).map(el=>el.text).filter(Boolean),['Каталог для читалок (OPDS)','Загружаю настройки каталога…']);
  await settle();
  assert.deepEqual(shelfSide.requests.sort(),[['GET','/api/devices'],['GET','/api/opds/menu']]);
  let view=see();
  assert.equal(visible(view,'code')[0].text,'http://books.waifu/opds');
  assert.deepEqual(signIn(view),['Этот адрес вводится в читалке. Вход — с логином и паролем синка, теми же, что для KOSync.']);
  const loadedRows=[
    'on | Читаю | книг: 3 | по последнему чтению | ↑ | ↓ | Читаю',
    'on | В планах | книг: 12 | по дате добавления | ↑ | ↓ | В планах',
    'on | Новые | книг: 5 | по дате добавления | ↑ | ↓ | добавлена за 30 дн. · первые 30',
    'on | Все | книг: 20 | по названию | ↑ | ↓ | все книги каталога',
    'off | Общее | книг: 5 | по дате добавления | ↑ | ↓ | Общее',
    'off | Нет на X3 | книг: 9 | по названию | ↑ | ↓ | нет на X3'];
  assert.deepEqual(menuRows(view),loadedRows);
  assert.deepEqual(view.filter(el=>el.tag==='option'&&el.scope.i===0).map(el=>el.text),
    ['по последнему чтению','по дате добавления','по названию','по автору','по изменению карточки']);
  const pageSize=view.find(el=>el.attrs['x-model']==='opdsDraft.page_size');
  assert.deepEqual([pageSize.bound['x-model'],pageSize.attrs.min,pageSize.attrs.max],[20,'5','60']);
  assert.deepEqual(view.filter(el=>el.disabled).map(el=>(el.scope.i??'')+el.text),['0↑','5↓','Сохранить','Вернуть по умолчанию']);
  assert.deepEqual(unsaved(),[false,false,false]);
  // A section made elsewhere may carry a language or a status this page has no word for: it is still described.
  assert.equal(menu.opdsFilterText({filter:{status:['reading','later'],started:true,language:'en'},limit:null}),'Читаю, later · начата · язык: en');
  console.log('PASS the OPDS block loads with the devices tab: catalogue address, sign-in line, page size and the sections in the server\'s order');

  // Each kind of edit is an unsaved change by itself, and «Отменить правки» takes it back.
  const edits=[()=>tick(field('s.enabled',4)),()=>fill(field('s.title',0),'Сейчас читаю'),()=>fill(field('s.sort',3),'author'),
    ()=>fill(field('opdsDraft.page_size'),'30'),()=>press(arrow(3,'↑')),()=>press(arrow(0,'↓'))];
  for (const edit of edits) {
    edit();assert.deepEqual(unsaved(),[true,true,true]);
    press(control('Отменить правки'));
    assert.deepEqual(unsaved(),[false,false,false]);assert.deepEqual(menuRows(see()),loadedRows);assert.equal(field('opdsDraft.page_size').bound['x-model'],20);
  }
  for (const edit of edits) edit();
  assert.deepEqual(menuRows(see()).slice(0,5),[
    'on | В планах | книг: 12 | по дате добавления | ↑ | ↓ | В планах',
    'on | Сейчас читаю | книг: 3 | по последнему чтению | ↑ | ↓ | Читаю',
    'on | Все | книг: 20 | по автору | ↑ | ↓ | все книги каталога',
    'on | Новые | книг: 5 | по дате добавления | ↑ | ↓ | добавлена за 30 дн. · первые 30',
    'on | Общее | книг: 5 | по дате добавления | ↑ | ↓ | Общее']);
  assert.deepEqual(unsaved(),[true,true,true]);
  // A built-in section can only be switched off: the row has no delete, and the component refuses as well.
  assert.deepEqual(visible(see(),'button','Удалить'),[]);
  menu.removeOpdsSection(0);assert.equal(menu.opdsDraft.sections.length,6);
  // The first section has nowhere to go up and the last nowhere to go down.
  const order=()=>plain(menu.opdsDraft.sections.map(s=>s.id)), before=order();
  menu.moveOpdsSection(0,-1);menu.moveOpdsSection(5,1);assert.deepEqual(order(),before);
  console.log('PASS switching, renaming, re-sorting and moving sections shows unsaved changes; built-in sections cannot be deleted');

  const blank={title:'',status:[],started:'',on_x3:'',added_days:'',limit:'',language:''};
  const input=model=>see().find(el=>el.shown&&el.attrs['x-model']===model);
  const statusBox=key=>see().find(el=>el.attrs['x-model']==='opdsNew.status'&&el.bound[':value']===key);
  press(control('Добавить раздел'));
  assert.deepEqual(see().filter(el=>el.shown&&el.attrs['x-text']==='opdsStatusLabels[key] || key').map(el=>el.text),['Читаю','В планах','Общее']);
  assert.deepEqual(see().filter(el=>el.shown&&el.tag==='option'&&el.scope.i===undefined).map(el=>el.text),
    ['Начата или нет — не важно','Начата','Не начата','Есть на X3 или нет — не важно','Есть на X3','Нет на X3']);
  press(control('Добавить'));
  assert.equal(menu.toasts.at(-1).message,'У раздела должно быть название');
  fill(input('opdsNew.title'),' Не начатые ');tick(statusBox('unsorted'));tick(statusBox('want'));
  fill(input('opdsNew.started'),'no');fill(input('opdsNew.on_x3'),'yes');fill(input('opdsNew.added_days'),'14');
  for (const wrong of ['0','-3','2.5']) {
    fill(input('opdsNew.limit'),wrong);press(control('Добавить'));
    assert.equal(menu.toasts.at(-1).message,'Дни и число книг — целые числа больше нуля');assert.equal(menu.opdsDraft.sections.length,6);
  }
  fill(input('opdsNew.limit'),'10');press(control('Добавить'));
  assert.equal(menuRows(see()).at(-1),'on | Не начатые | новый | по дате добавления | ↑ | ↓ | Удалить | В планах, Общее · не начата · есть на X3 · добавлена за 14 дн. · первые 10');
  assert.equal(control('Добавить'),undefined);assert.deepEqual(plain(menu.opdsNew),blank);
  // «Отмена» only hides the form: nothing is added, and what was typed is there when the form is opened again.
  press(control('Добавить раздел'));fill(input('opdsNew.title'),'Лишний');press(control('Отмена'));
  assert.equal(control('Добавить'),undefined);assert.equal(menu.opdsDraft.sections.length,7);
  // Nothing but a title: a section of the whole catalogue with no limit.
  press(control('Добавить раздел'));press(control('Добавить'));
  assert.equal(menuRows(see()).at(-1),'on | Лишний | новый | по дате добавления | ↑ | ↓ | Удалить | все книги каталога');
  // Only the sections the owner added can be deleted; deleting the second leaves the first.
  press(visible(see(),'button','Удалить').at(-1));
  assert.deepEqual(visible(see(),'button','Удалить').map(el=>el.scope.s.title),['Не начатые']);
  press(arrow(6,'↑'));
  console.log('PASS «Добавить раздел» builds a section from the server\'s statuses and the chosen filters; an added section can be deleted');

  shelfSide.requests.length=0;
  const saving=press(control('Сохранить'));
  // Nothing can be changed while the request is on its way.
  assert.deepEqual(see().filter(el=>el.shown&&['input','select','button'].includes(el.tag)&&!el.disabled),[]);
  menu.saveOpdsMenu();menu.resetOpdsMenu();
  await saving;
  assert.deepEqual(shelfSide.requests,[['PUT','/api/opds/menu',{page_size:30,sections:[
    {id:'want',title:'В планах',enabled:true,filter:{status:['want']},sort:'added',limit:null},
    {id:'reading',title:'Сейчас читаю',enabled:true,filter:{status:['reading']},sort:'read',limit:null},
    {id:'all',title:'Все',enabled:true,filter:{},sort:'author',limit:null},
    {id:'new',title:'Новые',enabled:true,filter:{added_days:30},sort:'added',limit:30},
    {id:'unsorted',title:'Общее',enabled:true,filter:{status:['unsorted']},sort:'added',limit:null},
    {title:'Не начатые',enabled:true,filter:{status:['want','unsorted'],started:false,on_x3:true,added_days:14},sort:'added',limit:10},
    {id:'no-x3',title:'Нет на X3',enabled:false,filter:{on_x3:false},sort:'title',limit:null}]}]]);
  assert.equal(shelfSide.headers['Content-Type'],'application/json');
  assert.deepEqual(messages(menu).at(-1),['Меню каталога сохранено','success']);
  // The answer replaces the draft: the new section has its id and its number of books, and nothing is unsaved.
  assert.equal(menuRows(see())[5],'on | Не начатые | книг: 2 | по дате добавления | ↑ | ↓ | Удалить | В планах, Общее · не начата · есть на X3 · добавлена за 14 дн. · первые 10');
  assert.equal(menu.opdsDraft.sections[5].id,'own5');assert.deepEqual(unsaved(),[false,false,false]);
  assert.equal(control('Вернуть по умолчанию').disabled,false);
  // What x-model leaves behind (a number typed as text, a space after the title) is not a change.
  fill(field('s.title',1),'Сейчас читаю ');fill(input('opdsDraft.page_size'),'30');
  assert.deepEqual(unsaved(),[false,false,false]);
  fill(field('s.title',1),'Сейчас читаю');
  shelfSide.requests.length=0;await menu.saveOpdsMenu();assert.deepEqual(shelfSide.requests,[]);
  // A GET asked before a save and answered after it carries the old menu: the page keeps what the save returned.
  // While that GET is on its way the menu is not asked for a second time.
  let answerNow;shelfSide.hold=new Promise(resolve=>answerNow=resolve);
  const overtaken=menu.loadOpdsMenu();shelfSide.hold=null;
  await menu.loadOpdsMenu();
  tick(field('s.enabled',6));await press(control('Сохранить'));
  answerNow();await overtaken;
  assert.deepEqual(shelfSide.requests.map(request=>request[0]),['GET','PUT']);
  assert.match(menuRows(see())[6],/^on \| Нет на X3 /);assert.equal(menu.opds.sections[6].enabled,true);
  assert.deepEqual([menu.opdsLoading,menu.opdsError,...unsaved()],[false,'',false,false,false]);
  console.log('PASS «Сохранить» sends the whole menu as the contract describes it, a new section without an id, and shows the server\'s answer');

  shelfSide.requests.length=0;
  for (const [size,title,said] of [['4','Все','Книг на странице — целое число от 5 до 60'],['61','Все','Книг на странице — целое число от 5 до 60'],
      ['','Все','Книг на странице — целое число от 5 до 60'],['12.5','Все','Книг на странице — целое число от 5 до 60'],
      ['30',' ','У каждого раздела должно быть название']]) {
    fill(input('opdsDraft.page_size'),size);fill(field('s.title',2),title);
    await press(control('Сохранить'));assert.equal(menu.toasts.at(-1).message,said);
  }
  assert.deepEqual(shelfSide.requests,[]);
  fill(field('s.title',2),'В планах');shelfSide.refuse=[400,'Названия разделов не должны повторяться.'];
  await press(control('Сохранить'));
  assert.equal(shelfSide.requests.length,1);assert.deepEqual(messages(menu).at(-1),['Названия разделов не должны повторяться.','error']);
  assert.equal(field('s.title',2).bound['x-model'],'В планах');assert.deepEqual(unsaved(),[true,true,true]);
  shelfSide.refuse=null;shelfSide.down=true;
  await press(control('Сохранить'));
  assert.equal(menu.toasts.at(-1).message,'Не удалось сохранить меню каталога: Failed to fetch');
  assert.equal(field('s.title',2).bound['x-model'],'В планах');assert.deepEqual(unsaved(),[true,true,true]);
  // Leaving the tab and coming back asks the server again: the numbers are new, the unsaved edits stay.
  shelfSide.down=false;builtinMenu[1].count=13;shelfSide.requests.length=0;
  menu.navigate('library');menu.navigate('devices');await settle();
  assert.ok(shelfSide.requests.some(request=>request[1]==='/api/opds/menu'));
  assert.deepEqual(menuRows(see()).slice(0,3).map(row=>row.split(' | ').slice(1,3)),[['В планах','книг: 13'],['Сейчас читаю','книг: 3'],['В планах','книг: 20']]);
  press(control('Отменить правки'));
  assert.equal(field('s.title',2).bound['x-model'],'Все');assert.deepEqual(unsaved(),[false,false,false]);
  // The same edit saved from another browser: on the next visit the draft is what the server has, and nothing is unsaved.
  fill(field('s.title',2),'Все книги');assert.deepEqual(unsaved(),[true,true,true]);
  shelfSide.stored.sections[2].title='Все книги';
  menu.navigate('library');menu.navigate('devices');await settle();
  assert.equal(field('s.title',2).bound['x-model'],'Все книги');assert.deepEqual(unsaved(),[false,false,false]);
  console.log('PASS a save the page or the server refuses says why and keeps the edits, and so does returning to the tab');

  fill(field('s.title',0),'Планы');shelfSide.requests.length=0;shelfSide.agree=false;
  await press(control('Вернуть по умолчанию'));
  assert.deepEqual(shelfSide.requests,[]);assert.match(shelfSide.asked,/^Вернуть меню каталога по умолчанию\?/);
  shelfSide.agree=true;shelfSide.down=true;
  await press(control('Вернуть по умолчанию'));
  assert.equal(menu.toasts.at(-1).message,'Не удалось вернуть меню по умолчанию: Failed to fetch');
  assert.equal(menu.opdsDraft.sections.length,7);assert.equal(field('s.title',0).bound['x-model'],'Планы');
  shelfSide.down=false;shelfSide.requests.length=0;
  const returning=press(control('Вернуть по умолчанию'));
  assert.deepEqual(see().filter(el=>el.shown&&['input','select','button'].includes(el.tag)&&!el.disabled),[]);
  await returning;
  assert.deepEqual(shelfSide.requests,[['DELETE','/api/opds/menu']]);
  assert.deepEqual(messages(menu).at(-1),['Меню каталога возвращено по умолчанию','success']);
  assert.deepEqual(menuRows(see()).map(row=>row.split(' | ').slice(0,2).join(' ')),['on Читаю','on В планах','on Новые','on Все','off Общее','off Нет на X3']);
  assert.equal(input('opdsDraft.page_size').bound['x-model'],20);
  // The built-in menu with nothing edited leaves nothing to save or to return.
  assert.deepEqual([...unsaved(),control('Вернуть по умолчанию').disabled],[false,false,false,true]);
  console.log('PASS «Вернуть по умолчанию» asks first, then DELETE brings back the built-in menu');

  const offShelf=opdsShelf(false,'/books/opds'), off=opdsPage(offShelf);
  off.navigate('devices');await settle();
  view=render(off,opdsBlock);
  assert.deepEqual(signIn(view),['Каталог выключен: не задан пароль синка (SYNC_PASSWORD). Меню можно настроить заранее.']);
  // The address is the page's own origin with the path the server names.
  assert.equal(visible(view,'code')[0].text,'http://books.waifu/books/opds');assert.match(menuRows(view)[4],/^off \| Общее \| книг: 5 /);
  // The menu of a catalogue that is off can still be edited and saved.
  tick(view.find(el=>el.attrs['x-model']==='s.enabled'&&el.scope.i===4));
  await press(render(off,opdsBlock).find(el=>el.text==='Сохранить'));
  assert.equal(offShelf.requests.at(-1)[0],'PUT');assert.equal(offShelf.stored.sections[4].enabled,true);
  // The server's part is missing or broken: the block says so and the rest of the tab works. X3 is not configured
  // here, so the catalogue heading is the only one left, which also shows that the block does not hang on X3.
  const lostShelf=opdsShelf(), lost=opdsPage(lostShelf);lostShelf.refuse=[404,'Not Found'];
  lost.navigate('devices');await settle();
  assert.deepEqual([lost.devicesError,lost.devicesLoading,lost.opdsLoading],['',false,false]);
  view=render(lost,opdsBlock);
  assert.deepEqual(view.filter(el=>el.shown).map(el=>el.text).filter(Boolean),['Каталог для читалок (OPDS)','Не удалось загрузить настройки каталога: Not Found','Повторить']);
  const tabView=render(lost,devicesTab), placed=text=>tabView.findIndex(el=>el.text===text);
  assert.deepEqual(visible(tabView,'h2').map(el=>el.text),['Каталог для читалок (OPDS)']);
  assert.ok(placed('На X3 сейчас')>=0&&placed('На X3 сейчас')<placed('Каталог для читалок (OPDS)'));
  lostShelf.refuse=null;
  await press(visible(view,'button','Повторить')[0]);
  assert.equal(lost.opdsError,'');assert.deepEqual(menuRows(render(lost,opdsBlock)).map(row=>row.split(' | ')[1]),['Читаю','В планах','Новые','Все','Общее','Нет на X3']);
  // A later refresh that fails keeps the loaded menu on the screen; «Обновить» of the tab asks for it too.
  lostShelf.down=true;lostShelf.requests.length=0;
  press(visible(render(lost,devicesTab),'button','Обновить')[0]);await settle();
  assert.ok(lostShelf.requests.some(request=>request[1]==='/api/opds/menu'));
  view=render(lost,opdsBlock);
  assert.equal(visible(view,'span')[0].text,'Не удалось загрузить настройки каталога: Failed to fetch');
  assert.deepEqual(menuRows(view).map(row=>row.split(' | ')[1]),['Читаю','В планах','Новые','Все','Общее','Нет на X3']);
  console.log('PASS without a sync password the block says the catalogue is off; a failed request leaves the tab working and can be repeated');
})().catch(error=>{console.error(error);process.exitCode=1;});
