/* 装机助手（手机优先）—— 配置单 + 实时总价。
 *
 * 这个页面的定位是**自用装机**，不是公开比价站，所以取舍很明确：
 *   · 只读取接口，不做任何写行的"采集"动作
 *   · 每件配件必须显示「这个价是哪天的」—— 京东每轮只采 2 个型号，
 *     进度比闲鱼落后很多，不标日期会把"上周的价"当成"现在的价"
 *   · 缺价的配件要显式提示，否则总价会悄悄偏低
 */
(function () {
  'use strict';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var listEl = $('#list');
  var toastEl = $('#toast');
  var freshEl = $('#fresh');
  var sheetHost = $('#sheet-host');

  var PLAT_ORDER = ['jd', 'pdd', 'xianyu'];
  var PLAT_LABEL = { jd: '京东', pdd: '拼多多', xianyu: '闲鱼' };
  var CAT_LABEL = {
    gpu: '显卡', cpu: 'CPU', ram: '内存', mb: '主板',
    ssd: '固态', psu: '电源', cooler: '散热', case: '机箱'
  };

  var state = { builds: [], open: {} };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function money(v) {
    if (v == null) return '—';
    return '¥' + Math.round(v).toLocaleString('zh-CN');
  }

  function relDate(d) {
    if (!d) return '';
    var t = new Date(d + 'T00:00:00');
    var now = new Date();
    now.setHours(0, 0, 0, 0);
    var days = Math.round((now - t) / 86400000);
    if (days <= 0) return '今天';
    if (days === 1) return '昨天';
    return days + '天前';
  }

  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { toastEl.hidden = true; }, 2400);
  }

  function api(path, opts) {
    var init = opts || {};
    init.headers = { 'Content-Type': 'application/json' };
    return fetch(path, init).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.detail || ('请求失败 ' + res.status));
        });
      }
      return res.json();
    });
  }

  /* ---------------------------------------------------------------- 渲染 */

  function platCells(item) {
    var cells = item.platforms || {};
    var bestCode = item.best ? item.best.code : null;
    var html = '';
    PLAT_ORDER.forEach(function (code) {
      var cell = cells[code];
      if (!cell) {
        html += '<span class="bk-plat none">' + PLAT_LABEL[code] + ' —</span>';
        return;
      }
      var cls = code === bestCode ? 'bk-plat cheap' : 'bk-plat';
      html += '<span class="' + cls + '">' + PLAT_LABEL[code] +
        ' <b>' + esc(money(cell.price)) + '</b></span>';
    });
    return html;
  }

  function itemHtml(item) {
    var cat = CAT_LABEL[item.category] || item.category || '';
    var best = item.best;
    var bestHtml = best
      ? '<span class="bk-best">' + esc(money(best.price)) + '</span>'
      : '<span class="bk-best miss">暂无报价</span>';
    var sub = best
      ? PLAT_LABEL[best.code] + ' · ' + esc(relDate(best.date)) +
        (item.quantity > 1 ? ' · ' + item.quantity + ' 件' : '')
      : '';
    // 稳健价之外还有明显更低的挂牌 → 提示出来但**不计入总价**。
    // 实测 RTX 5060 Ti 16G 有一条 ¥2300，实际卖的是 RX 6800（标题里
    // 写了"换了 5060Ti 故出"）—— 这类错配会让整套配件的总价凭空便宜两千。
    var warn = (best && best.low != null && best.price != null && best.low < best.price * 0.85)
      ? '<div class="bk-note bk-warn">另有 ' + esc(money(best.low)) +
        ' 的挂牌，未计入总价（低于行情 15% 以上，多为错配或引流）</div>'
      : '';
    return '' +
      '<div class="bk-item" data-item="' + item.item_id + '">' +
        '<div class="bk-item-top">' +
          (cat ? '<span class="bk-cat">' + esc(cat) + '</span>' : '') +
          '<span class="bk-model">' + esc(item.model) + '</span>' +
          bestHtml +
        '</div>' +
        (sub ? '<div class="bk-note">' + sub + '</div>' : '') +
        '<div class="bk-plats">' + platCells(item) + '</div>' +
        warn +
        '<div class="bk-item-foot">' +
          (item.note ? '<span class="bk-note">' + esc(item.note) + '</span>' : '') +
          '<span class="spacer"></span>' +
          '<span class="bk-qty">' +
            '<button data-act="dec" aria-label="减少">−</button>' +
            '<span>' + item.quantity + '</span>' +
            '<button data-act="inc" aria-label="增加">＋</button>' +
          '</span>' +
          '<button class="bk-del" data-act="del">删除</button>' +
        '</div>' +
      '</div>';
  }

  function buildHtml(b) {
    var opened = !!state.open[b.id];
    var head;
    if (b.item_count === 0) {
      head = '<div class="bk-total"><b>—</b><span>还没加配件</span></div>';
    } else if (b.total == null) {
      head = '<div class="bk-total"><b>—</b><span class="bk-warn">' + b.item_count + ' 件 · 全部缺价</span></div>';
    } else {
      var miss = b.missing_count > 0
        ? ' · <span class="bk-warn">' + b.missing_count + ' 件缺价</span>'
        : ' · 全齐';
      head = '<div class="bk-total"><b>' + esc(money(b.total)) + '</b><span>' +
        b.item_count + ' 件' + miss + '</span></div>';
    }
    return '' +
      '<section class="bk-card' + (opened ? ' open' : '') + '" data-build="' + b.id + '">' +
        '<div class="bk-card-head" data-act="toggle">' +
          '<div class="bk-name">' + esc(b.name) +
            (b.note ? '<small>' + esc(b.note) + '</small>' : '') +
          '</div>' +
          head +
          '<span class="bk-caret">▶</span>' +
        '</div>' +
        '<div class="bk-items">' +
          (b.items.length ? b.items.map(itemHtml).join('') :
            '<div class="bk-empty">还没有配件</div>') +
          '<div class="bk-addrow"><button class="bk-btn" data-act="add">＋ 加配件</button></div>' +
          '<div class="bk-addrow" style="padding-top:0">' +
            '<button class="bk-btn" data-act="rename">重命名</button> ' +
            '<button class="bk-btn" data-act="remove-build" style="margin-left:8px">删除这套配置</button>' +
          '</div>' +
        '</div>' +
      '</section>';
  }

  function render() {
    if (!state.builds.length) {
      listEl.innerHTML = '<div class="bk-empty">还没有配置单。<br>点下面的「新建配置单」开始。</div>';
      return;
    }
    listEl.innerHTML = state.builds.map(buildHtml).join('');
    var now = new Date();
    freshEl.textContent = ' · ' + String(now.getHours()).padStart(2, '0') + ':' +
      String(now.getMinutes()).padStart(2, '0') + ' 刷新';
  }

  function findByBuild(id) {
    for (var i = 0; i < state.builds.length; i++) {
      if (String(state.builds[i].id) === String(id)) return state.builds[i];
    }
    return null;
  }

  function replaceBuild(updated) {
    for (var i = 0; i < state.builds.length; i++) {
      if (String(state.builds[i].id) === String(updated.id)) {
        state.builds[i] = updated;
        return;
      }
    }
    state.builds.push(updated);
  }

  /* ---------------------------------------------------------------- 加载 */

  function load(showToast) {
    return api('/api/builds').then(function (data) {
      state.builds = data.items || [];
      render();
      if (showToast) toast('已刷新');
    }).catch(function (err) { toast('加载失败：' + err.message); });
  }

  /* ---------------------------------------------------------------- 弹层 */

  function closeSheet() { sheetHost.innerHTML = ''; }

  function openSheet(title, bodyHtml, footHtml) {
    sheetHost.innerHTML = '' +
      '<div class="bk-mask" data-mask="1">' +
        '<div class="bk-sheet" role="dialog">' +
          '<h2>' + esc(title) + '</h2>' +
          '<div class="bk-body">' + bodyHtml + '</div>' +
          (footHtml ? '<div class="bk-sheet-foot">' + footHtml + '</div>' : '') +
        '</div>' +
      '</div>';
    return $('.bk-sheet', sheetHost);
  }

  function promptText(title, value) {
    return new Promise(function (resolve) {
      openSheet(
        title,
        '<input class="bk-input" id="sheet-input" value="' + esc(value || '') + '" maxlength="64">',
        '<button class="bk-btn" data-sheet="cancel">取消</button>' +
        '<button class="bk-btn primary" data-sheet="ok">确定</button>'
      );
      var input = $('#sheet-input');
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      sheetHost.onclick = function (ev) {
        var act = ev.target.getAttribute && ev.target.getAttribute('data-sheet');
        if (act === 'cancel') { closeSheet(); sheetHost.onclick = null; resolve(null); }
        if (act === 'ok') {
          var v = $("#sheet-input").value.trim();
          closeSheet(); sheetHost.onclick = null; resolve(v);
        }
      };
      input.onkeydown = function (ev) {
        if (ev.key === 'Enter') {
          var v = input.value.trim();
          closeSheet(); sheetHost.onclick = null; resolve(v);
        }
      };
    });
  }

  /* 加配件：搜索型号 → 点选 */
  function openAddItem(buildId) {
    var body = '' +
      '<input class="bk-input" id="hit-q" placeholder="搜型号，如 7800X3D / 5060 Ti" autocomplete="off">' +
      '<div class="bk-hits" id="hits"><div class="bk-empty">输入关键词开始搜索</div></div>';
    openSheet('加配件', body, '<button class="bk-btn" data-sheet="cancel">关闭</button>');
    var q = $('#hit-q');
    var hits = $('#hits');
    var timer = null;

    function search() {
      var kw = q.value.trim();
      if (kw.length < 1) {
        hits.innerHTML = '<div class="bk-empty">输入关键词开始搜索</div>';
        return;
      }
      api('/api/products?limit=25&q=' + encodeURIComponent(kw))
        .then(function (data) {
          var items = (data.items || []).filter(function (r) { return r.product_id; });
          if (!items.length) {
            hits.innerHTML = '<div class="bk-empty">没有匹配的型号</div>';
            return;
          }
          hits.innerHTML = items.map(function (r) {
            var price = r.latest == null
              ? '<span class="p miss">无行情</span>'
              : '<span class="p">' + esc(money(r.latest)) + '</span>';
            return '<div class="bk-hit" data-pid="' + r.product_id + '">' +
              '<div class="m">' + esc(r.model) +
                '<small>' + esc(CAT_LABEL[r.category] || '') + ' · ' + esc(r.spec || '') + '</small>' +
              '</div>' + price + '</div>';
          }).join('');
        })
        .catch(function (err) { hits.innerHTML = '<div class="bk-empty">搜索失败：' + esc(err.message) + '</div>'; });
    }

    q.oninput = function () { clearTimeout(timer); timer = setTimeout(search, 220); };
    hits.onclick = function (ev) {
      var row = ev.target.closest ? ev.target.closest('[data-pid]') : null;
      if (!row) return;
      var pid = row.getAttribute('data-pid');
      api('/api/builds/' + buildId + '/items', {
        method: 'POST',
        body: JSON.stringify({ product_id: Number(pid) })
      }).then(function (updated) {
        replaceBuild(updated);
        state.open[updated.id] = true;
        render();
        toast('已加入');
        closeSheet();
      }).catch(function (err) { toast('加入失败：' + err.message); });
    };
    sheetHost.onclick = function (ev) {
      var act = ev.target.getAttribute && ev.target.getAttribute('data-sheet');
      if (act === 'cancel' || ev.target.getAttribute && ev.target.getAttribute('data-mask')) {
        closeSheet(); sheetHost.onclick = null;
      }
    };
    setTimeout(function () { q.focus(); }, 60);
  }

  /* ---------------------------------------------------------------- 事件 */

  listEl.addEventListener('click', function (ev) {
    var t = ev.target;
    var card = t.closest ? t.closest('[data-build]') : null;
    if (!card) return;
    var buildId = card.getAttribute('data-build');
    var build = findByBuild(buildId);
    var act = t.getAttribute && t.getAttribute('data-act');

    // 「点头部**任意位置**都能展开/收起」。
    //
    // ⚠️ 不能只认 `data-act="toggle"` 那个元素的 getAttribute ——
    // 手指点在手机上落到的往往是里面的名字或价格，那些**子元素没有 data-act**，
    // 于是 `act` 为空、处理器直接 return，点击静默失效。
    // （实测：Playwright 点 .bk-card-head 的中心正好落在 .bk-name 上，
    //   连点三次都不展开，而控制台一条错都不报。）
    // 用 closest 往上找，命中与否才代表"点在了头部区域"。
    if (t.closest && t.closest('[data-act="toggle"]')) {
      state.open[buildId] = !state.open[buildId];
      render();
      return;
    }
    if (!act) return;
    ev.stopPropagation();

    if (act === 'add') { openAddItem(buildId); return; }

    if (act === 'rename') {
      promptText('重命名配置单', build ? build.name : '').then(function (name) {
        if (!name) return;
        api('/api/builds/' + buildId, { method: 'PATCH', body: JSON.stringify({ name: name }) })
          .then(function (updated) { replaceBuild(updated); render(); })
          .catch(function (err) { toast('改名失败：' + err.message); });
      });
      return;
    }

    if (act === 'remove-build') {
      if (!confirm('删除配置单「' + (build ? build.name : '') + '」？')) return;
      api('/api/builds/' + buildId, { method: 'DELETE' })
        .then(function () { load(); toast('已删除'); })
        .catch(function (err) { toast('删除失败：' + err.message); });
      return;
    }

    var itemEl = t.closest ? t.closest('[data-item]') : null;
    if (!itemEl || !build) return;
    var itemId = itemEl.getAttribute('data-item');
    var item = null;
    for (var i = 0; i < build.items.length; i++) {
      if (String(build.items[i].item_id) === String(itemId)) { item = build.items[i]; break; }
    }
    if (!item) return;

    if (act === 'del') {
      api('/api/build-items/' + itemId, { method: 'DELETE' })
        .then(function (updated) { replaceBuild(updated); state.open[buildId] = true; render(); toast('已移除'); })
        .catch(function (err) { toast('移除失败：' + err.message); });
      return;
    }
    if (act === 'inc' || act === 'dec') {
      var next = item.quantity + (act === 'inc' ? 1 : -1);
      if (next < 1 || next > 99) return;
      api('/api/build-items/' + itemId, { method: 'PATCH', body: JSON.stringify({ quantity: next }) })
        .then(function (updated) { replaceBuild(updated); state.open[buildId] = true; render(); })
        .catch(function (err) { toast('修改失败：' + err.message); });
    }
  });

  $('#refresh').onclick = function () { load(true); };

  $('#new-build').onclick = function () {
    promptText('新建配置单', '我的配置').then(function (name) {
      if (!name) return;
      api('/api/builds', { method: 'POST', body: JSON.stringify({ name: name }) })
        .then(function (created) {
          state.builds.push(created);
          state.open[created.id] = true;
          render();
          openAddItem(created.id);
        })
        .catch(function (err) { toast('新建失败：' + err.message); });
    });
  };

  load();
})();
