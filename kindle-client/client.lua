-- Network work runs in a single worker. capture never uses HTTP or cc.db.
local root=os.getenv('KINDLE_SYNC_HOME') or '/mnt/us/kindle-sync'
local json=dofile(root..'/lib/json.lua')
local codec=assert(loadfile(root..'/bin/kindle-pos'))('kindle_pos_module')
local function q(s) return "'"..tostring(s):gsub("'","'\\''").."'" end
local function read(path) local f=io.open(path,'rb');if not f then return nil end;local s=f:read('*a');f:close();return s end
local function write(path,s) local f=assert(io.open(path,'wb'));assert(f:write(s));assert(f:close()) end
local function ok(cmd) local a=os.execute(cmd);return a==true or a==0 end
local function out(cmd) local p=assert(io.popen(cmd));local s=p:read('*a');local a=p:close();if a~=true and a~=0 then error('Command failed: '..cmd) end;return s:gsub('%s+$','') end
local function log(s) io.stderr:write(os.date('%Y-%m-%d %H:%M:%S')..' '..s..'\n') end
local function mkdir(p) assert(ok('mkdir -p '..q(p)),'mkdir failed') end
local state=root..'/state/agent'
mkdir(state..'/pending');mkdir(state..'/epoch');mkdir(root..'/maps')
local function tmp() return os.tmpname() end
local function atomic(path,data) local p=path..'.new';write(p,data);assert(os.rename(p,path)) end
local function load(path,default) local s=read(path);if not s then return default end;return json.decode(s) end
local function save(path,data) atomic(path,json.encode(data)) end
-- Server reset counter per book; baseline is the local PID at reset, never uploaded as-is.
local function epoch_state(key) return load(state..'/epoch/'..key..'.json',{epoch=0}) end
local function safe(s) return type(s)=='string' and s:match('^[A-Za-z0-9_-]+$') and #s<=100 end
-- Kindle BusyBox sleep rejects fractions; usleep takes microseconds.
local function sleep(s) assert(ok('usleep '..math.floor(s*1000000)),'usleep failed') end
local function prop(name) return out('lipc-get-prop com.lab126.appmgrd '..name..' 2>/dev/null') end
local function context_path()
  local c=prop('activeContext');local path=c:match('file://(/.*)')
  return path and path:gsub('%%(%x%x)',function(hex) return string.char(tonumber(hex,16)) end)
end
local function active_path()
  if prop('activeApp')=='com.lab126.booklet.reader' then return context_path() end
end
local function inventory() return load(state..'/inventory.json',{}) end
local function book_at(path)
  if not path then return nil end
  for _,b in ipairs(inventory()) do if b.path==path then return b end end
end
local function observe()
  local path=active_path()
  if path then atomic(state..'/last-reader',path) end
end
local function find_yjf(b)
  local dir=b.path:gsub('%.[^%.]+$','')..'.sdr'
  local paths=out('find '..q(dir).." -maxdepth 1 -type f -name '*.yjf' 2>/dev/null")
  if paths=='' or paths:find('\n') then error('Expected one YJF: '..b.key) end
  return paths
end
local function native(b)
  local s=assert(read(find_yjf(b)),'Cannot read YJF')
  local parsed,err=codec.parse_store(s);assert(parsed,err)
  local point=assert(codec.best_position(parsed),'YJF has no current position')
  return point,s
end
local function wait_flush(b)
  local previous=nil;local stable=0
  for _=1,16 do
    local point,bytes=native(b)
    if bytes==previous then stable=stable+1 else stable=0 end
    if stable>=3 then return point end
    previous=bytes;sleep(0.3)
  end
  error('YJF did not settle after reader closed: '..b.key)
end
local function close_reader(b)
  if active_path()==b.path then
    assert(ok("lipc-set-prop com.lab126.appmgrd start 'app://com.lab126.KPPMainApp?view=KPP_LIBRARY'"),'Cannot open library')
    for _=1,20 do
      if prop('activeApp')=='com.lab126.KPPMainApp' then return wait_flush(b) end
      sleep(0.2)
    end
    error('Reader did not close; no position written')
  end
  return wait_flush(b)
end
local function local_lock(fn)
  local lock=state..'/local.lock'
  for _=1,40 do
    if ok('mkdir '..q(lock)..' 2>/dev/null') then
      local success,result=pcall(fn)
      assert(ok('rmdir '..q(lock)))
      if not success then error(result) end
      return result
    end
    sleep(0.2)
  end
  error('Local operation busy; retry on wake')
end
local function record(b,point)
  -- One latest local candidate, replaced on each close. No historical copies.
  local previous=load(state..'/pending/'..b.key..'.json')
  -- The point held back after a reset is not a peak to preserve.
  if not previous or previous.fingerprint~=b.fingerprint or point.pid>=previous.pid or previous.pid==epoch_state(b.key).baseline then
    save(state..'/pending/'..b.key..'.json',{pid=point.pid,long=point.long,fingerprint=b.fingerprint})
  end
  atomic(state..'/last-captured',b.key)
end
local function capture(key)
  local b
  if key then for _,v in ipairs(inventory()) do if v.key==key then b=v end end
  else b=book_at(active_path() or read(state..'/last-reader')) end
  if not b then log('capture: no cached book; run scan first');return end
  local_lock(function() local p=close_reader(b);record(b,p);log('captured '..b.key..' PID '..p.pid) end)
end

local function configured()
  local c=load(root..'/config.json')
  assert(c and type(c.server)=='string' and c.server:match('^https?://[^%s]+$'),'Set config.json server URL')
  c.server=c.server:gsub('/+$','')
  assert(type(c.api_key)=='string' and c.api_key:match('^[A-Za-z0-9_-]+$'),'Set API key (letters, digits, - and _)')
  assert(safe(c.device_id),'Invalid device_id')
  return c
end
local function http(c,method,path,payload,rawfile,extra)
  local response=tmp();local request=tmp();local config=tmp()
  -- curl config is supported by older Kindle builds. Never expose the token in argv.
  local hs='Authorization: Bearer '..c.api_key..'\n'..(extra or '')
  local lines={}
  for h in hs:gmatch('[^\n]+') do lines[#lines+1]='header = "'..h..'"' end
  write(config,table.concat(lines,'\n')..'\n')
  local cmd='curl --silent --show-error --connect-timeout 3 --max-time '..(rawfile and '180' or '8')..
    ' -X '..method..' -K '..q(config)..' -o '..q(response).." -w '%{http_code}' "
  if payload then
    write(request,json.encode(payload));cmd=cmd..' -H '..q('Content-Type: application/json')..' --data-binary '..q('@'..request)
  elseif rawfile then cmd=cmd..' --data-binary '..q('@'..rawfile) end
  local success,code=pcall(out,cmd..' '..q(c.server..path))
  local data=read(response)
  os.remove(config);os.remove(request);os.remove(response)
  assert(success,'Server unavailable; local position retained')
  assert(tonumber(code) and tonumber(code)>=200 and tonumber(code)<300,'HTTP '..tostring(code)..': '..tostring(data):sub(1,200))
  return data
end
local function api(c,method,path,payload) return json.decode(http(c,method,path,payload)) end
local function basename(p) return p:match('([^/]+)$') end
local function collect_files(path)
  local parent=assert(path:match('^(.*)/[^/]+$'));local main=basename(path)
  assert(not main:find('[\r\n]') and main:sub(1,1)~='-','Unsupported filename')
  local files={main};local sdr=main:gsub('%.[^%.]+$','')..'.sdr'
  local listing=out('cd '..q(parent)..' && if [ -d '..q(sdr)..' ]; then find '..q(sdr)..' -type f; fi')
  for file in listing:gmatch('[^\n]+') do
    local ext=file:match('%.([^%.]+)$');ext=ext and ext:lower()
    if ext=='kfx' or ext=='azw' or ext=='azw8' or ext=='azw9' or ext=='res' or ext=='yj' or ext=='md' then files[#files+1]=file end
  end
  table.sort(files)
  return parent,files
end
local function fingerprint(parent,files)
  local p=tmp();local checks={}
  for _,file in ipairs(files) do checks[#checks+1]=out('cd '..q(parent)..' && sha256sum '..q(file)) end
  write(p,table.concat(checks,'\n')..'\n');local digest=out('sha256sum '..q(p)):match('^([a-f0-9]+)');os.remove(p)
  assert(digest and #digest==64,'sha256sum unavailable');return digest
end
-- Cheap enough for every sync: one cc.db query and file sizes, no hashing.
local function list_local()
  local rows={};local seen={}
  for _,row in ipairs(codec.query_books()) do
    if safe(row.key) and not seen[row.key] then
      local path=row.location
      if os.getenv('KINDLE_POS_BOOK_ROOT') then path=os.getenv('KINDLE_POS_BOOK_ROOT')..'/'..basename(path) end
      local f=io.open(path,'rb')
      if f then
        rows[#rows+1]={key=row.key,title=row.title,path=path,size=f:seek('end')};f:close()
        seen[row.key]=true
      end
    end
  end
  return rows
end
local function same_books(rows,books)
  if #rows~=#books then return false end
  local known={}
  for _,b in ipairs(books) do known[b.key]=b end
  for _,r in ipairs(rows) do
    local b=known[r.key]
    if not b or b.path~=r.path or b.size~=r.size or b.title~=r.title then return false end
  end
  return true
end
-- Hashing every book is slow on the Kindle. With previous, only new or resized
-- books are hashed; without it (periodic full scan) everything is.
local function scan_local(rows,previous)
  local known={}
  for _,b in ipairs(previous or {}) do known[b.key]=b end
  local books={}
  for _,r in ipairs(rows or list_local()) do
    local old=known[r.key]
    local digest=old and old.path==r.path and old.size==r.size and old.fingerprint
    if not digest then local parent,files=collect_files(r.path);digest=fingerprint(parent,files) end
    books[#books+1]={key=r.key,title=r.title,path=r.path,size=r.size,fingerprint=digest}
  end
  save(state..'/inventory.json',books);return books
end
local function bundle(b)
  local parent,files=collect_files(b.path);local list=tmp();local archive=tmp()
  write(list,table.concat(files,'\n')..'\n')
  local success=ok('tar -cf '..q(archive)..' -C '..q(parent)..' -T '..q(list))
  os.remove(list);assert(success,'tar failed')
  if fingerprint(parent,files)~=b.fingerprint then os.remove(archive);error('Book changed during upload; scan again') end
  return archive
end
local function download_map(c,b,m)
  local data=http(c,'GET','/api/books/'..b.key..'/map')
  local mapping,err=codec.parse_position_map(data,b.key);assert(mapping,err)
  atomic(root..'/maps/'..b.key..'.posmap',data)
  save(state..'/'..b.key..'.map.json',{fingerprint=m.fingerprint,asset_version=m.asset_version})
  log('downloaded map '..b.key)
end
local function map_valid(b)
  local data=read(root..'/maps/'..b.key..'.posmap');if not data then return nil end
  local map,err=codec.parse_position_map(data,b.key);assert(map,err);return map
end
local function checked_native(b,map)
  local p=native(b)
  local expected=codec.coordinate_for_pid(map,p.pid)
  local valid=expected==p.long
  if not valid then
    local dec=codec.decode_long(p.long)
    if dec then
      for _,row in ipairs(map.rows) do
        if dec.eid==row.eid and dec.offset>=row.offset and dec.offset<=row.offset+row.length and row.pid+dec.offset-row.offset==p.pid then valid=true;break end
      end
    end
  end
  assert(valid,'YJF LONG/PID disagrees with map: '..b.key)
  return p
end
local function synchronize(c,b,m)
  local prefix='/api/books/'..b.key
  local remote=api(c,'GET',prefix..'/position')
  assert(remote.asset_version==m.asset_version,'Map changed; retry')
  if read(state..'/suspended') then return end
  local map=assert(map_valid(b),'Missing map')
  local remote_epoch=remote.epoch or 0
  local e
  local decision=local_lock(function()
    if read(state..'/suspended') then return nil end
    local path=active_path()
    local current=checked_native(b,map)
    local candidate=load(state..'/pending/'..b.key..'.json')
    if candidate and candidate.fingerprint~=b.fingerprint then candidate=nil end
    e=epoch_state(b.key)
    if e.epoch~=remote_epoch then
      -- Position was reset on the server: drop the offline peak and hold back
      -- the old local point until the reader actually moves.
      os.remove(state..'/pending/'..b.key..'.json');candidate=nil
      e={epoch=remote_epoch,baseline=current.pid};save(state..'/epoch/'..b.key..'.json',e)
      log('reset '..b.key..': local PID '..current.pid..' held back until moved')
    end
    if candidate and candidate.pid~=e.baseline and candidate.pid>(remote.pid or -1) and candidate.pid>current.pid then
      -- Preserve the furthest offline capture if the reader later moved backward.
      return {pid=candidate.pid}
    end
    -- A higher remote position may require closing this reader. Re-read after flush:
    -- the native in-memory position can be further ahead than its YJF snapshot.
    if remote.pid and remote.pid>current.pid and path==b.path then
      current=close_reader(b);current=checked_native(b,map);record(b,current)
      candidate={pid=current.pid}
    end
    if read(state..'/suspended') then return nil end
    if remote.pid and remote.pid>current.pid then
      if active_path()==b.path then error('Reader reopened; apply deferred') end
      local lua=os.getenv('KINDLE_SYNC_LUA') or root..'/bin/luajit'
      local cmd=q(lua)..' '..q(root..'/bin/kindle-pos')..' set-pid '..q(b.key)..' '..tostring(remote.pid)..' '..q(root..'/maps/'..b.key..'.posmap')
      assert(ok(cmd..' >/dev/null'),'kindle-pos failed')
      local actual=checked_native(b,map)
      assert(actual.pid==remote.pid,'YJF readback mismatch')
      log('applied '..b.key..' PID '..remote.pid)
      return {ack=true,pid=actual.pid,revision=remote.revision}
    end
    -- Capture current closed-book state, including initial setup. Never upload
    -- a stale on-disk snapshot while the book is still open.
    if path~=b.path then candidate={pid=current.pid} end
    if candidate and candidate.pid==e.baseline then return nil end
    if candidate and candidate.pid>=(remote.pid or -1) then
      if candidate.pid>(remote.pid or -1) then return {pid=candidate.pid} end
      return {ack=true,pid=current.pid,revision=remote.revision}
    end
  end)
  if not decision then return end
  if decision.ack then
    if decision.pid==remote.pid then api(c,'POST',prefix..'/applied',{device_id=c.device_id,pid=decision.pid,revision=decision.revision}) end
  else
    local result=api(c,'PUT',prefix..'/position',{pid=decision.pid,asset_version=m.asset_version,epoch=remote_epoch})
    if result.accepted and e.baseline then e.baseline=nil;save(state..'/epoch/'..b.key..'.json',e) end
    log((result.accepted and 'published ' or 'not accepted ('..tostring(result.reason)..') ')..b.key..' PID '..decision.pid)
  end
end
local function sync(scan)
  local c=configured();local books=inventory()
  local stamp=tonumber(read(state..'/last-scan') or '0')
  if scan or #books==0 or os.time()-stamp>600 then
    books=scan_local();atomic(state..'/last-scan',tostring(os.time()))
  else
    -- New, removed or re-downloaded books reach the server on this sync, not after the next full scan.
    local rows=list_local()
    if not same_books(rows,books) then books=scan_local(rows,books);log('library changed: '..#books..' books') end
  end
  local request_books=books
  -- rxi/json encodes an empty Lua table as []; backend expects an array.
  api(c,'POST','/api/kindle/inventory',{device_id=c.device_id,books=request_books})
  for _,b in ipairs(books) do
    if read(state..'/suspended') then break end
    local success,err=pcall(function()
      local prefix='/api/books/'..b.key
      local m=api(c,'GET',prefix..'/manifest')
      if m.needs_upload then
        local file=bundle(b)
        local success,result=pcall(http,c,'PUT',prefix..'/bundle',nil,file,'X-Source-Fingerprint: '..b.fingerprint..'\n')
        os.remove(file);assert(success,result);log('uploaded '..b.key);return
      end
      if m.map_status~='ready' then log('map '..b.key..': '..m.map_status);return end
      local localmap=load(state..'/'..b.key..'.map.json',{})
      if localmap.fingerprint~=m.fingerprint or localmap.asset_version~=m.asset_version or not read(root..'/maps/'..b.key..'.posmap') then download_map(c,b,m) end
      synchronize(c,b,m)
    end)
    if not success then
      log(b.key..': '..tostring(err))
      -- Still downloading or replaced in place: hash everything again on the next sync.
      if tostring(err):find('Book changed during upload',1,true) then os.remove(state..'/last-scan') end
    end
  end
end
local function push()
  -- Best effort immediately after cover close. Cached data only: no cc.db,
  -- book scan, library mutation, map upload, or map generation on this path.
  local key=read(state..'/last-captured')
  if not safe(key) then return end
  local candidate=load(state..'/pending/'..key..'.json')
  local metadata=load(state..'/'..key..'.map.json')
  if not candidate or not metadata or metadata.fingerprint~=candidate.fingerprint then return end
  local e=epoch_state(key)
  if candidate.pid==e.baseline then log('close push '..key..': unchanged since reset');return end
  local c=configured()
  local result=api(c,'PUT','/api/books/'..key..'/position',{pid=candidate.pid,asset_version=metadata.asset_version,epoch=e.epoch})
  if result.accepted and e.baseline then e.baseline=nil;save(state..'/epoch/'..key..'.json',e) end
  log('close push '..key..': '..(result.accepted and 'accepted' or tostring(result.reason)))
end
local function doctor(local_only)
  configured()
  for _,cmd in ipairs({'curl','tar','sha256sum','sqlite3','lipc-get-prop','lipc-set-prop','lipc-wait-event'}) do
    assert(ok('command -v '..cmd..' >/dev/null'),'Missing '..cmd)
  end
  if local_only then log('OK: local dependencies and config');return end
  local response=api(configured(),'GET','/api/library')
  log('OK: server accessible, '..#response.books..' registered books; '..#inventory()..' cached local books')
end
local cmd=arg[1] or 'help'
local success,err=pcall(function()
  if cmd=='capture' then capture(arg[2])
  elseif cmd=='observe' then observe()
  elseif cmd=='sleep' then atomic(state..'/suspended','1');capture()
  elseif cmd=='wake' then os.remove(state..'/suspended')
  elseif cmd=='sync' or cmd=='scan' then
    if read(state..'/suspended') then log('suspended; sync deferred');return end
    local lock=state..'/worker.lock'
    if not ok('mkdir '..q(lock)..' 2>/dev/null') then log('sync already running');return end
    local success,err=pcall(sync,cmd=='scan');assert(ok('rmdir '..q(lock)))
    if not success then error(err) end
  elseif cmd=='push' then push()
  elseif cmd=='doctor' then doctor(arg[2]=='--local')
  else print('kindle-agent doctor | scan | sync | capture [KEY] | start | stop | status') end
end)
if not success then log(tostring(err));os.exit(1) end
