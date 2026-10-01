<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>影视仓 & OK影视 - 官方配置分发</title>
    <!-- HarmonyOS Sans SC 字体（本地） -->
    <link rel="stylesheet" href="static/fonts/HarmonyOS_Sans_SC/index-core.css?v=3">
    <!-- 字体异步加载：首屏只需 Regular/Medium（index-core.css），Semibold/Bold 延后加载 -->
    <link rel="preload" as="style" href="static/fonts/HarmonyOS_Sans_SC/index-extra.css?v=3" onload="this.onload=null;this.rel='stylesheet'">
    <noscript><link rel="stylesheet" href="static/fonts/HarmonyOS_Sans_SC/index-extra.css?v=3"></noscript>
    <!-- HarmonyOS Symbol 图标字体（本地 HMSymbol.ttf） -->
    <link rel="stylesheet" href="static/css/hmsymbol-icons.css">
    <link rel="stylesheet" href="static/css/harmony.css">
  <link rel="stylesheet" href="static/css/theme-dark.css">
    
    
<style>
/* ====== Quick stats summary ====== */
.lp-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 40px;
}
.lp-summary-item {
  background: rgba(255,255,255,.04);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--hmo-border);
  border-radius: var(--hmo-radius-lg);
  padding: 24px 20px;
  text-align: center;
  transition: all .25s;
}
.lp-summary-item:hover {
  transform: translateY(-2px);
  box-shadow: var(--hmo-shadow-md);
}
.lp-summary-icon {
  font-size: 1.6rem;
  color: var(--hmo-primary);
  margin-bottom: 8px;
}
.lp-summary-value {
  font-size: 1.6rem;
  font-weight: 700;
  color: var(--hmo-text);
  line-height: 1.2;
}
.lp-summary-label {
  font-size: .82rem;
  color: var(--hmo-text-secondary);
  margin-top: 4px;
}

@media (max-width: 768px) {
  .lp-summary { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 480px) {
  .lp-summary { grid-template-columns: 1fr; }
}

/* ====== Compact FAQ preview ====== */
.lp-faq-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.lp-faq-item {
  background: rgba(255,255,255,.04);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--hmo-border);
  border-radius: var(--hmo-radius);
  padding: 16px 20px;
  transition: all .2s;
  cursor: pointer;
}
.lp-faq-item:hover {
  box-shadow: var(--hmo-shadow-sm);
  border-color: var(--hmo-primary-light);
}
.lp-faq-q {
  font-weight: 600;
  font-size: .92rem;
  color: var(--hmo-text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.lp-faq-q .hms {
  color: var(--hmo-primary);
  font-size: 14px;
}
.lp-faq-a {
  font-size: .85rem;
  color: var(--hmo-text-secondary);
  margin-top: 8px;
  line-height: 1.6;
  display: none;
}
.lp-faq-item.open .lp-faq-a {
  display: block;
}
</style>

</head>
<body>
    

    

<main class="main-content main-content--landing">
        



<!-- ====== Top Navigation Bar ====== -->
<nav class="lp-topnav">
  <div class="lp-topnav-inner">
    <a href="index.html" class="lp-topnav-brand">
      <img src="img/icon-ysc.svg" alt="影视仓" class="brand-logo"><img src="img/icon-ok.svg" alt="OK影视" class="brand-logo">
      <span>影视仓&amp;OK影视</span>
    </a>
    <button type="button" class="lp-topnav-toggle" id="lpNavToggle" aria-label="切换菜单">
      <i class="hms hms-menu"></i>
    </button>
    <div class="lp-topnav-links" id="lpNavLinks">
      <a href="index.html" class="active"><i class="hms hms-house"></i> 主页</a>
      <a href="download.html"><i class="hms hms-download"></i> 下载</a>
      <a href="tutorial.html"><i class="hms hms-book_closed_checkmark"></i> 教程</a>
      <a href="faq.html"><i class="hms hms-questionmark_circle"></i> FAQ</a>
      <span class="lp-topnav-spacer"></span>
      
    </div>
  </div>
</nav>

<!-- ====== Main content container ====== -->
<div class="lp-page">

  <!-- ====== Hero Section ====== -->
  <section class="lp-hero">
    <div class="lp-hero-bg">
      <div class="lp-hero-circle lp-hero-circle-1"></div>
      <div class="lp-hero-circle lp-hero-circle-2"></div>
      <div class="lp-hero-circle lp-hero-circle-3"></div>
    </div>
    <div class="lp-hero-inner">
      <span class="lp-hero-tag"><i class="hms hms-TV_tv_fill"></i> 影视仓 &amp; OK影视 配置分发</span>
      <h1>影视仓 &amp; OK影视 配置分发</h1>
      <p class="lp-hero-sub">纯净、安全、持续更新的配置服务，一键填入 APP 即可畅享海量影视资源。无需复杂操作，即开即用。</p>
      
      <div class="lp-hero-config">
        <span class="lp-hero-config-label"><i class="hms hms-link"></i> 配置地址（复制后在 APP 中填入）</span>
        <div class="cfg-list">
          <div class="lp-hero-config-box">
            <span class="cfg-badge">1</span>
            <code id="configUrlDisplay">http://www.影视仓.com</code>
            <button type="button" class="lp-hero-config-copy" onclick="copyConfigUrl(this)">
              <i class="hms hms-doc_text"></i> 复制
            </button>
          </div>
          <div class="lp-hero-config-box">
            <span class="cfg-badge">2</span>
            <code>http://www.ok影视.com</code>
            <button type="button" class="lp-hero-config-copy" onclick="copyConfigUrl(this)">
              <i class="hms hms-doc_text"></i> 复制
            </button>
          </div>
          <div class="lp-hero-config-box">
            <span class="cfg-badge">3</span>
            <code>http://www.影视仓.net</code>
            <button type="button" class="lp-hero-config-copy" onclick="copyConfigUrl(this)">
              <i class="hms hms-doc_text"></i> 复制
            </button>
          </div>
          <div class="lp-hero-config-box">
            <span class="cfg-badge">防封</span>
            <code>https://700sjro44343.vicp.fun/240731/one/</code>
            <button type="button" class="lp-hero-config-copy" onclick="copyConfigUrl(this)">
              <i class="hms hms-doc_text"></i> 复制
            </button>
          </div>
        </div>
      </div>

      <div class="lp-hero-actions">
        
        <a href="tutorial.html" class="lp-btn lp-btn-outline"><i class="hms hms-book_closed"></i> 查看教程</a>
        <a href="download.html" class="lp-btn lp-btn-outline"><i class="hms hms-download"></i> 下载 APP</a>
      </div>
    </div>
  </section>

  <!-- ====== Features Section ====== -->
  <section class="lp-section">
    <div class="lp-section-header">
      <h2>为什么选择我们</h2>
      <p>稳定可靠 · 持续更新 · 简单易用</p>
    </div>
    <div class="lp-features-grid">
      <div class="lp-feature-card">
        <div class="lp-feature-icon" style="background:var(--hmo-primary-bg);color:var(--hmo-primary);">
          <i class="hms hms-arrow_2_circlepath"></i>
        </div>
        <h3>持续自动更新</h3>
        <p>配置每日自动同步上游，确保资源地址始终有效，无需手动维护。</p>
      </div>
      <div class="lp-feature-card">
        <div class="lp-feature-icon" style="background:var(--hmo-success-bg);color:var(--hmo-success);">
          <i class="hms hms-shield_checkered"></i>
        </div>
        <h3>安全可靠</h3>
        <p>多重安全防护机制，IP 风险检测与自动封禁，保障服务稳定运行。</p>
      </div>
      <div class="lp-feature-card">
        <div class="lp-feature-icon" style="background:var(--hmo-warning-bg);color:#B87600;">
          <i class="hms hms-link"></i>
        </div>
        <h3>一键配置</h3>
        <p>复制配置地址，打开影视仓 APP 填入即可，无需任何复杂设置。</p>
      </div>
      <div class="lp-feature-card">
        <div class="lp-feature-icon" style="background:#F0F0FF;color:#6C5CE7;">
          <i class="hms hms-book_closed"></i>
        </div>
        <h3>教程支持</h3>
        <p>提供详细图文教程，从安装到配置分步引导，新手也能快速上手。</p>
      </div>
    </div>
  </section>

  <!-- ====== How It Works ====== -->
  <section class="lp-section lp-section-alt">
    <div class="lp-section-header">
      <h2>三步开始使用</h2>
      <p>简单三步，畅享海量影视资源</p>
    </div>
    <div class="lp-steps">
      <div class="lp-step">
        <div class="lp-step-number">1</div>
        <div class="lp-step-icon"><i class="hms hms-download"></i></div>
        <h3>下载影视仓 APP</h3>
        <p>在官网下载页获取最新版影视仓 APP，安装到您的设备上。</p>
      </div>
      <div class="lp-step-connector"></div>
      <div class="lp-step">
        <div class="lp-step-number">2</div>
        <div class="lp-step-icon"><i class="hms hms-doc_text"></i></div>
        <h3>复制配置地址</h3>
        <p>点击上方复制按钮，将配置地址保存到剪贴板中。</p>
      </div>
      <div class="lp-step-connector"></div>
      <div class="lp-step">
        <div class="lp-step-number">3</div>
        <div class="lp-step-icon"><i class="hms hms-TV_tv_fill"></i></div>
        <h3>填入 APP 畅享</h3>
        <p>打开影视仓 APP，在设置中粘贴配置地址，即可开始使用。</p>
      </div>
    </div>
  </section>

  <!-- ====== Editable content (if any) ====== -->
  

  <!-- ====== FAQ Preview ====== -->
  <section class="lp-section">
    <div class="lp-section-header">
      <h2>常见问题</h2>
      <p>快速了解影视仓配置服务</p>
    </div>
    <div class="lp-faq-list">
      <div class="lp-faq-item" onclick="this.classList.toggle('open')">
        <div class="lp-faq-q"><i class="hms hms-questionmark_circle"></i> 什么是配置地址？</div>
        <div class="lp-faq-a">配置地址是影视仓 APP 获取影视资源列表的入口链接。填入配置地址后，APP 会自动加载我们维护的影视资源，无需手动添加任何源。</div>
      </div>
      <div class="lp-faq-item" onclick="this.classList.toggle('open')">
        <div class="lp-faq-q"><i class="hms hms-questionmark_circle"></i> 配置地址会失效吗？</div>
        <div class="lp-faq-a">我们会持续维护并自动更新配置，确保资源地址长期有效。如果遇到问题，请重新复制配置地址并在 APP 中更新。</div>
      </div>
      <div class="lp-faq-item" onclick="this.classList.toggle('open')">
        <div class="lp-faq-q"><i class="hms hms-questionmark_circle"></i> 需要付费使用吗？</div>
        <div class="lp-faq-a">基础配置服务完全免费。如需更多高级功能或专属配置，可以通过注册用户中心获取验证码解锁更多服务。</div>
      </div>
      <div class="lp-faq-item" onclick="this.classList.toggle('open')">
        <div class="lp-faq-q"><i class="hms hms-questionmark_circle"></i> 如何获取帮助？</div>
        <div class="lp-faq-a">访问我们的<a href="tutorial.html" style="color:var(--hmo-primary);">教程页面</a>查看详细图文指导，或在<a href="faq.html" style="color:var(--hmo-primary);">FAQ 页面</a>查看更多常见问题解答。</div>
      </div>
    </div>
  </section>

  <!-- ====== CTA Section ====== -->
  <section class="lp-cta">
    <div class="lp-cta-bg"></div>
    <div class="lp-cta-inner">
      <h2>立即开始使用</h2>
      <p>复制配置地址，打开影视仓 APP，一步到位畅享精彩影视内容。</p>
      <div class="lp-hero-actions" style="justify-content:center;">
        <button type="button" class="lp-btn lp-btn-primary" onclick="copyConfigUrl(this)">
          <i class="hms hms-doc_text"></i> 复制配置地址
        </button>
        <a href="tutorial.html" class="lp-btn lp-btn-outline"><i class="hms hms-book_closed"></i> 查看教程</a>
      </div>
    </div>
  </section>

</div><!-- /.lp-page -->




    </main>

    <footer class="app-footer app-footer--landing">
        <small>影视仓 &amp; OK影视@2026 <a href="admin.html" class="footer-admin">管理入口</a></small>
    </footer>

    <!-- Custom dialog replacements (native confirm/alert crash Trae preview) -->
    <dialog id="_confirmDialog">
        <article style="min-width:320px;max-width:420px;">
            <header><strong id="_confirmTitle">确认</strong></header>
            <p id="_confirmText" style="margin:var(--hmo-space-sm) 0;"></p>
            <footer style="margin-top:var(--hmo-space-md);display:flex;justify-content:flex-end;gap:var(--hmo-space-sm);">
                <button id="_confirmCancel" class="outline">取消</button>
                <button id="_confirmOk" class="contrast">确定</button>
            </footer>
        </article>
    </dialog>
    <dialog id="_alertDialog">
        <article style="min-width:320px;max-width:420px;">
            <header><strong>提示</strong></header>
            <p id="_alertText" style="margin:var(--hmo-space-sm) 0;"></p>
            <footer style="margin-top:var(--hmo-space-md);display:flex;justify-content:flex-end;">
                <button id="_alertOk" class="contrast">确定</button>
            </footer>
        </article>
    </dialog>

    <!-- Admin Notification Dialog -->
    <dialog id="adminNotifyDialog">
        <article style="min-width:380px;max-width:520px;width:90vw;">
            <header>
                <strong><i class="hms hms-info_circle_fill"></i> 通知</strong>
            </header>
            <div style="margin:var(--hmo-space-sm) 0;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--hmo-space-sm);">
                    <span style="font-size:13px;color:var(--hmo-text-tertiary);">点击通知可标记为已读</span>
                    <a href="#" id="readAllBtn" style="color:var(--hmo-primary,#409eff);font-size:13px;">全部标为已读</a>
                </div>
                <ul id="notifyList" class="notify-list" style="list-style:none;padding:0;margin:0;max-height:420px;overflow-y:auto;">
                    <li style="padding:var(--hmo-space-sm);text-align:center;color:var(--hmo-text-tertiary);">加载中…</li>
                </ul>
            </div>
            <footer style="margin-top:var(--hmo-space-md);display:flex;justify-content:flex-end;">
                <button id="notifyDialogClose" class="outline" onclick="document.getElementById('adminNotifyDialog').close()">关闭</button>
            </footer>
        </article>
    </dialog>

    

    

    

    <script>
// Universal sidebar toggle (mobile)
(function(){
  var t=document.getElementById('sidebarToggle');
  var s=document.querySelector('.sidebar');
  var o=document.getElementById('sidebarOverlay');
  if(!t||!s)return;
  function c(){s.classList.remove('open');if(o)o.classList.remove('open');}
  t.addEventListener('click',function(){s.classList.toggle('open');if(o)o.classList.toggle('open');});
  if(o)o.addEventListener('click',c);
  s.querySelectorAll('.sidebar-nav a').forEach(function(a){a.addEventListener('click',c);});
})();
</script>

<script>
// ====== User logout handler ======
document.addEventListener('DOMContentLoaded', function() {
  var logoutLink = document.querySelector('.lp-topnav-logout');
  if (logoutLink) {
    logoutLink.addEventListener('click', function(e) {
      e.preventDefault();
      fetch('/api/v1/user/logout', { method: 'POST' })
        .catch(function() {})
        .finally(function() { window.location.href = '/'; });
    });
  }
  // Mobile nav toggle
  var toggle = document.getElementById('lpNavToggle');
  var links = document.getElementById('lpNavLinks');
  if (toggle && links) {
    toggle.addEventListener('click', function() {
      links.classList.toggle('open');
    });
  }
});

// ====== Copy config URL ======
function copyConfigUrl(btn) {
  var code = null;
  if (btn && btn.closest) { var box = btn.closest('.lp-hero-config-box'); if (box) code = box.querySelector('code'); }
  if (!code) code = document.getElementById('configUrlDisplay');
  if (!code) return;
  var text = code.textContent;
  var ok = false;
  try { ok = fallbackCopy(text); } catch(e) {}
  if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
  }
  showCopied(btn);
}
function fallbackCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch(e) {}
  document.body.removeChild(ta);
  return ok;
}
function showCopied(btn) {
  var btns = btn ? [btn] : document.querySelectorAll('.lp-hero-config-copy, .lp-btn-primary[onclick*="copyConfigUrl"]');
  btns.forEach(function(btn) {
    if (btn.classList.contains('lp-hero-config-copy')) {
      btn.innerHTML = '<i class="hms hms-checkmark"></i> 已复制';
      btn.style.background = 'rgba(0,181,120,.25)';
      setTimeout(function() {
        btn.innerHTML = '<i class="hms hms-doc_text"></i> 复制';
        btn.style.background = '';
      }, 2000);
    } else {
      var orig = btn.innerHTML;
      btn.innerHTML = '<i class="hms hms-checkmark"></i> 已复制';
      setTimeout(function() { btn.innerHTML = orig; }, 2000);
    }
  });
}


</script>

</body>
</html>