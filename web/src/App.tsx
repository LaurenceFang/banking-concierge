import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronsLeft, ChevronsRight, CreditCard, MessageCircle, Plus, ReceiptText, UserRound, RefreshCw, ChartNoAxesCombined, House, CalendarDays, X, Maximize2, ArrowLeft } from "lucide-react";
import { createConversation, getConversation, getConversations, getOverview, markRead, renameConversation } from "./api";
import type { Action, Conversation, ConversationDetail, Overview } from "./types";
import { AccountOverview } from "./components/AccountOverview";
import { ChatWorkspace } from "./components/ChatWorkspace";
import { ActionDrawer } from "./components/ActionDrawer";
import { BillingPage } from "./components/BillingPage";
import { PaymentsPage } from "./components/PaymentsPage";
import { SubscriptionsPage } from "./components/SubscriptionsPage";
import { CardsPage } from "./components/CardsPage";
import { WealthPage } from "./components/WealthPage";
import { sections, routeFromHash, routeHash, type Domain } from "./workspace";
import { ConversationTitle, ConversationMenu, useRailResize, type ConversationMenuTarget } from "./components/SidebarControls";
const nav = [{id:"accounts",label:"账户",icon:CreditCard},{id:"bills",label:"账单",icon:ReceiptText},{id:"wealth",label:"财富",icon:ChartNoAxesCombined},{id:"life",label:"生活",icon:CalendarDays}] as const;
const read = (key:string) => {try{return localStorage.getItem(key);}catch{return null;}};
const save = (key:string,value:string) => {try{localStorage.setItem(key,value);}catch{}};
export default function App() {
  const rail = useRailResize();
  const [contextMenu, setContextMenu] = useState<ConversationMenuTarget | null>(null);
  const closeContextMenu = useCallback(() => setContextMenu(null), []);
  const [route,setRoute] = useState(()=>routeFromHash(location.hash));
  const routeRef=useRef(route); routeRef.current=route;
  useEffect(() => setContextMenu(null), [route.scope, route.conversation]);
  const scope=route.scope; const domain=scope?.split("/")[0] as Domain|undefined; const section=scope?.split("/")[1];
  const [collapsed,setCollapsed]=useState(()=>read("yx2.rail.v1")==="closed" || innerWidth<760);
  const [conversations,setConversations]=useState<Conversation[]>([]);
  const [main,setMain]=useState<Conversation|null>(null);
  const mainRef=useRef<Conversation|null>(null);
  const [listLoading,setListLoading]=useState(true);
  const [listScope,setListScope]=useState<string|undefined>(undefined);
  const previousScope=useRef(scope);
  const listVersion=useRef(0); const loadVersion=useRef(0);
  const [activeId,setActiveId]=useState(""); const activeRef=useRef("");
  const [detail,setDetail]=useState<ConversationDetail|null>(null);
  const [pending,setPending]=useState<Action|null>(null);
  const [drawer,setDrawer]=useState(false);
  const dismissed=useRef(new Set<string>((() => {try {const values=JSON.parse(read("yx2.closed.v2")??"[]");return Array.isArray(values)?values.filter((x):x is string=>typeof x==="string"):[];}catch{return [];}})()));
  const [overview,setOverview]=useState<Overview|null>(null);
  const [loading,setLoading]=useState(true);const [overviewError,setOverviewError]=useState("");const [error,setError]=useState("");
  const [chatBusy,setChatBusy]=useState(false);const [decisionBusy,setDecisionBusy]=useState(false);const [actionBusy,setActionBusy]=useState(false);
  const [revision,setRevision]=useState(0);const [renaming,setRenaming]=useState("");const [title,setTitle]=useState("");
  const [profileOpen,setProfileOpen]=useState(false);
  const go=useCallback((nextScope?:string,id="",expanded=false)=>{location.hash=routeHash(nextScope,id,expanded);},[]);
  useEffect(()=>{const changed=()=>setRoute(routeFromHash(location.hash));window.addEventListener("hashchange",changed);return()=>window.removeEventListener("hashchange",changed);},[]);
  const refreshOverview=useCallback(async()=>{setLoading(true);try{setOverview(await getOverview());setOverviewError("");}catch(e){setOverviewError(e instanceof Error?e.message:"无法读取账户。");}finally{setLoading(false);}},[]);
  const refreshList=useCallback(async()=>{const v=++listVersion.current;const requested=routeRef.current.scope;try{const result=await getConversations(requested);if(v!==listVersion.current || requested!==routeRef.current.scope)return;setConversations(result.conversations);setListScope(requested);const m=result.conversations.find(x=>x.kind==="main");if(m){mainRef.current=m;setMain(m);}setListLoading(false);}catch(e){if(v===listVersion.current){setListLoading(false);setError(e instanceof Error?e.message:"无法读取对话列表。");}}},[]);
  const acceptAction=useCallback((action:Action|null)=>{const live=action?.status==="pending"?action:null;setPending(live);if(live && !dismissed.current.has(`${activeRef.current}:${live.id}`))setDrawer(true);},[]);
  const loadConversation=useCallback(async(id:string)=>{const v=++loadVersion.current;try{const result=await getConversation(id);if(v!==loadVersion.current || activeRef.current!==id)return;setDetail(result);acceptAction(result.pending_action);setError("");await markRead(id);void refreshList();}catch(e){if(v===loadVersion.current)setError(e instanceof Error?e.message:"无法读取对话。");}},[acceptAction,refreshList]);
  useEffect(()=>{void refreshOverview();},[refreshOverview]);
  useEffect(()=>{if(previousScope.current!==scope){setCollapsed(false);previousScope.current=scope;}setConversations([]);setListLoading(true);setDrawer(false);setActionBusy(false);void refreshList();},[scope,refreshList]);
  useEffect(()=>{
    const id=route.conversation || (!scope?main?.id??"":"");
    if(id===activeRef.current)return;
    ++loadVersion.current;activeRef.current=id;setActiveId(id);setDetail(null);setPending(null);setDrawer(false);setChatBusy(false);setDecisionBusy(false);setActionBusy(false);setError("");
    if(id.startsWith("compose:"))setDetail({conversation:{id,title:"新对话",kind:"temporary",updated_at:new Date().toISOString(),unread_count:0,scopes:scope?[scope]:[]},messages:[],pending_action:null});
    else if(id)void loadConversation(id);
  },[route.conversation,scope,main?.id,loadConversation]);
  useEffect(()=>{const timer=window.setInterval(()=>{if(document.hidden)return;void refreshList();const id=activeRef.current;if(id && !id.startsWith("compose:") && !chatBusy && !decisionBusy && !actionBusy && !renaming && !read(`yx2.draft.${id}`))void loadConversation(id);},20000);return()=>clearInterval(timer);},[refreshList,loadConversation,chatBusy,decisionBusy,actionBusy,renaming]);
  const home=()=>{setCollapsed(false);go(undefined,mainRef.current?.id);};
  const create=(draft="")=>{const id=`compose:${crypto.randomUUID()}`;if(draft)save(`yx2.draft.${id}`,draft);go(scope,id);if(innerWidth<760)setCollapsed(true);};
  const closeDrawer=()=>{if(pending){dismissed.current.add(`${activeId}:${pending.id}`);save("yx2.closed.v2",JSON.stringify([...dismissed.current].slice(-200)));}setDrawer(false);};
  useEffect(()=>{const escape=(event:KeyboardEvent)=>{if(event.key!=="Escape")return;if(drawer)closeDrawer();else if(scope && route.conversation)go(scope);};window.addEventListener("keydown",escape);return()=>window.removeEventListener("keydown",escape);},[drawer,pending,activeId,scope,route.conversation,go]);
  function refreshCurrent(){void refreshOverview();void refreshList();if(activeRef.current && !activeRef.current.startsWith("compose:"))void loadConversation(activeRef.current);setRevision(v=>v+1);}
  async function ensureConversation(){const expected=scope;const item=await createConversation(scope,crypto.randomUUID());if(routeRef.current.scope===expected){activeRef.current=item.id;setActiveId(item.id);setDetail(null);go(scope,item.id);}void refreshList();return item.id;}
  function pageAction(action:Action){if(action.conversation_id!==activeRef.current)return;acceptAction(action);if(action.conversation_id)void loadConversation(action.conversation_id);}
  async function rename(){if(!title.trim())return;try{await renameConversation(renaming,title.trim());setRenaming("");void refreshList();if(activeRef.current===renaming)void loadConversation(renaming);}catch(e){setError(e instanceof Error?e.message:"改名失败。");}}
  const visibleConversations=listScope===scope?conversations:[];
  const current=detail?.conversation??conversations.find(x=>x.id===activeId);
  const business=!!scope && !route.expanded;
  const chatVisible=!!detail && (!business || !!route.conversation);
  const heading=business?nav.find(x=>x.id===domain)?.label:current?.kind==="main"?"主对话":current?.title??"主对话";
  const pageProps={conversationId:activeId,ensureConversation,revision,blocked:chatBusy||decisionBusy,onBusy:setActionBusy,onAction:pageAction};
  const chat=detail && <ChatWorkspace key={activeId} detail={detail} scope={scope} pending={pending} onAction={action=>{if(activeRef.current===activeId)acceptAction(action);}} onOpen={()=>setDrawer(true)} onRefresh={()=>{if(activeRef.current===activeId)refreshCurrent();}} onCreated={id=>{if(activeRef.current===activeId){go(scope,id,routeRef.current.expanded);void refreshList();}}} onBusy={value=>{if(activeRef.current===activeId){if(value)++loadVersion.current;setChatBusy(value);}}} decisionBusy={decisionBusy||actionBusy}/>;
  return <div style={rail.style} className={`app-shell ${collapsed?"rail-collapsed":""} ${rail.dragging?"rail-resizing":""}`}>
    <nav className="nav-rail" aria-label="主导航">
      <div className="rail-brand"><button className="brand-home" onClick={home} aria-label="返回主页"><House size={21}/>{!collapsed && <span>银小二</span>}</button><button className="icon-button" aria-label={collapsed?"展开会话栏":"收起会话栏"} onClick={()=>{setCollapsed(!collapsed);save("yx2.rail.v1",collapsed?"open":"closed");}}>{collapsed?<ChevronsRight size={18}/>:<ChevronsLeft size={18}/>}</button></div>
      <button className="new-conversation" aria-label="新建对话" onClick={()=>create()}><Plus size={20}/>{!collapsed && <span>新建对话</span>}</button>
      <button className={`conversation-select main-conversation ${!scope && current?.kind==="main"?"selected":""}`} onClick={home} title="主对话"><MessageCircle size={18}/>{!collapsed && <span>主对话</span>}</button>
      <div className="conversation-list">{!collapsed && <p className="rail-caption">{scope?sections[domain!].find(([id])=>id===section)?.[1]:"全部对话"}</p>}
        {listLoading && !collapsed && <p className="rail-empty" role="status">正在读取…</p>}
        {visibleConversations.filter(x=>x.kind!=="main").map(item=><div key={item.id} className={`conversation-row ${activeId===item.id?"selected":""}`}>
          {renaming===item.id?<form className="rename-form" onKeyDown={e=>{if(e.key==="Escape"){e.stopPropagation();setRenaming("");}}} onSubmit={e=>{e.preventDefault();void rename();}}><input autoFocus aria-label="对话名称" value={title} onChange={e=>setTitle(e.target.value)} maxLength={80}/><button>保存</button><button type="button" onClick={()=>setRenaming("")}>取消</button></form>:<><button className="conversation-select" title={item.title} aria-label={item.title} aria-haspopup="menu" onContextMenu={e=>{e.preventDefault();setContextMenu({id:item.id,title:item.title,x:e.clientX,y:e.clientY,trigger:e.currentTarget});}} onKeyDown={e=>{if(e.key==="ContextMenu" || (e.shiftKey && e.key==="F10")){e.preventDefault();const rect=e.currentTarget.getBoundingClientRect();setContextMenu({id:item.id,title:item.title,x:rect.left+20,y:rect.bottom,trigger:e.currentTarget});}}} onClick={()=>{go(scope,item.id);if(innerWidth<760)setCollapsed(true);}}>{!collapsed && <ConversationTitle title={item.title}/>}{item.unread_count>0 && <small className="unread">{item.unread_count}</small>}</button></>}
        </div>)}
        {!listLoading && !collapsed && !visibleConversations.some(x=>x.kind!=="main") && <p className="rail-empty">{scope?"这里还没有对话。":"还没有普通对话。"}<br/>有想聊的事，随时新建。</p>}
      </div>
      <div className="utility-nav">{nav.map(({id,label,icon:Icon})=><button key={id} className={domain===id?"selected":""} aria-current={domain===id?"page":undefined} title={label} onClick={()=>{setCollapsed(false);go(`${id}/${sections[id][0][0]}`);}}><Icon size={19}/>{!collapsed && <span>{label}</span>}</button>)}</div>
      <button className="profile profile-button" onClick={()=>setProfileOpen(!profileOpen)} aria-expanded={profileOpen} title="个人资料"><span className="avatar"><UserRound size={19}/></span>{!collapsed && <span>{overview?.user.name??"个人资料"}</span>}</button>{profileOpen && <div className="profile-details">{overview?.user.name??"正在读取"}<p>模拟银行账户</p><button className="text-button" onClick={()=>setProfileOpen(false)}>关闭</button></div>}
      {!collapsed && rail.separator}
    </nav>
    {contextMenu && <ConversationMenu target={contextMenu} onClose={closeContextMenu} onRename={()=>{setRenaming(contextMenu.id);setTitle(contextMenu.title);setContextMenu(null);}}/>}
    {!collapsed && <button className="rail-backdrop" aria-label="关闭会话导航" onClick={()=>setCollapsed(true)}/>}
    <main className="main-shell"><header className={`app-header ${!business ? "chat-page-header" : ""}`}><div className="workspace-heading">{scope && route.expanded && <button className="icon-button" aria-label="返回业务页" onClick={()=>go(scope,activeId)}><ArrowLeft size={19}/></button>}<h1 title={heading}>{!business ? <ConversationTitle title={heading ?? "主对话"}/> : heading}</h1></div><div className="header-tools"><button className="icon-button" aria-label="刷新当前内容" disabled={chatBusy||decisionBusy||actionBusy} onClick={refreshCurrent}><RefreshCw size={17}/></button><span>模拟银行</span></div></header>
      {error && <div className="shell-error" role="alert">{error}<button className="text-button" onClick={refreshCurrent}>重试</button></div>}
      {business && <nav className="section-tabs" aria-label={`${heading}二级导航`}>{sections[domain!].map(([id,label])=><button key={id} className={id===section?"active":""} aria-current={id===section?"page":undefined} onClick={()=>go(`${domain}/${id}`)}>{label}</button>)}</nav>}
      <div className={`workspace-layout ${chatVisible && business && !drawer?"with-chat-panel":""}`}>
        <div className={`workspace-content ${!business && detail ? "hidden" : ""}`}>
          {!business ? <p className="loading-copy">{error?"暂时无法读取对话。":"正在恢复对话…"}</p> : domain==="bills" ? section==="payments"?<PaymentsPage overview={overview} onDiscuss={create}/>:<BillingPage revision={revision} mode={section as "entries"|"analysis"|"import"}/> : domain==="accounts"?section==="cards"?<CardsPage {...pageProps}/>:<div className="data-page"><AccountOverview overview={overview} loading={loading} error={overviewError} refresh={()=>void refreshOverview()} expanded accountsOnly={section==="accounts"}/></div> : domain==="wealth"?<WealthPage {...pageProps} section={section} accounts={overview?.accounts??[]} onRefresh={refreshCurrent}/>:<SubscriptionsPage {...pageProps}/>}
        </div>
        {chatVisible && <aside className={`chat-panel ${!business?"full-chat":""} ${drawer && business?"hidden":""}`} aria-label={business?"对话侧面板":"完整对话"}>{business && <header className="panel-header"><h2>{current?.title??"新对话"}</h2><button className="icon-button" aria-label="放大对话" onClick={()=>go(scope,activeId,true)}><Maximize2 size={17}/></button><button className="icon-button" aria-label="关闭对话面板" onClick={()=>go(scope)}><X size={18}/></button></header>}{chat}</aside>}
        {pending && drawer && <div className="confirmation-slot"><button className="text-button back-to-chat" onClick={closeDrawer}><ArrowLeft size={15}/> 返回对话</button><ActionDrawer key={`${activeId}:${pending.id}`} action={pending} accounts={overview?.accounts??[]} conversationId={activeId} chatBusy={chatBusy||actionBusy} onClose={closeDrawer} onBusy={setDecisionBusy} onDone={()=>{setPending(null);setDrawer(false);setDecisionBusy(false);refreshCurrent();}}/></div>}
      </div>
    </main>
  </div>;
}
