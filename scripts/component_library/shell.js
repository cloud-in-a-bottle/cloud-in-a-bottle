(function () {
  var data = JSON.parse(document.getElementById('lib-data').textContent);
  var main = document.getElementById('lib-main');
  var nav = document.getElementById('lib-nav');
  var filter = document.getElementById('lib-filter');
  var toastEl = document.getElementById('lib-toast');

  var FOUNDATIONS = [
    {id: 'colors', name: 'Colors', group: 'Foundations', test: /^--(bg|fg|interactive|color)-/},
    {id: 'type', name: 'Type', group: 'Foundations', test: /^--(text|font|weight|leading|tracking)-/},
    {id: 'space', name: 'Space and shape', group: 'Foundations', test: /^--(space|radius|border|shadow|layout|grass)/},
    {id: 'icons', name: 'Icons', group: 'Foundations'},
  ];
  var SURFACES = {Sky: 'var(--bg-sky)', White: 'var(--bg-primary)', Panel: 'var(--bg-surface)'};

  var state = {width: 'Desktop', surface: 'Sky', tab: 'Usage', query: ''};
  var frameCount = 0;
  var frames = {};
  var said = null;
  var activeId = null;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (value == null || value === false) return;
      if (key === 'text') node.textContent = value;
      else if (key === 'class') node.className = value;
      else if (key.slice(0, 2) === 'on') node.addEventListener(key.slice(2), value);
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    });
    [].concat(children == null ? [] : children).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(child.nodeType ? child : document.createTextNode(String(child)));
    });
    return node;
  }

  function toast(text) {
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { toastEl.hidden = true; }, 1600);
  }

  function copy(text, what) {
    function done() { toast('Copied ' + what); }
    function fallback() {
      var area = el('textarea', {style: 'position:fixed;opacity:0'});
      area.value = text;
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      area.remove();
      toast(ok ? 'Copied ' + what : 'Select the text to copy it');
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else fallback();
  }

  function withAssets(html) {
    return html.replace(/\/static\/img\/[\w\/.-]+\.svg/g, function (path) {
      return data.assets[path] || path;
    });
  }

  function escapeHtml(text) {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&#34;');
  }

  function escapeJinja(text) {
    return text.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }

  function fill(text, values, escape) {
    return text.replace(/@@(\w+)@@/g, function (match, name) {
      return name in values ? escape(String(values[name])) : match;
    });
  }

  // The preview runs in a frame so the product's media queries see the width picked
  // here, and its element styles stay out of the library's own controls.
  var FRAME_SCRIPT = function (id) {
    return "var ID=" + JSON.stringify(id) + ";" +
      "function report(){parent.postMessage({libFrame:ID,height:document.documentElement.scrollHeight},'*')}" +
      "new ResizeObserver(report).observe(document.getElementById('root'));" +
      "window.addEventListener('message',function(e){var m=e.data;if(!m||!('html' in m))return;" +
      "document.getElementById('root').innerHTML=m.html;document.body.style.background=m.surface;report()});" +
      "document.addEventListener('click',function(e){" +
      "var a=e.target.closest&&e.target.closest('a[href]');" +
      "if(a){e.preventDefault();parent.postMessage({libFrame:ID,said:'This link goes to '+a.getAttribute('href')},'*');return}" +
      "var b=e.target.closest&&e.target.closest('button');" +
      "if(b&&!b.closest('.info-tip,.nav-menu'))parent.postMessage({libFrame:ID,said:'Clicked '+(b.getAttribute('aria-label')||b.textContent.trim())},'*')});" +
      "document.addEventListener('submit',function(e){e.preventDefault()});" +
      "parent.postMessage({libFrame:ID,ready:true},'*');";
  };

  function frameDoc(id, padding) {
    var css = data.productCss +
      'html{scrollbar-gutter:auto}body{min-height:0;padding:' + padding + 'px;background:transparent}';
    return '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Inter:wght@400;500&display=swap">' +
      '<style>' + css + '</style></head><body><div id="root"></div>' +
      '<script>' + data.frameScripts + '<\/script><script>' + FRAME_SCRIPT(id) + '<\/script></body></html>';
  }

  function makeFrame(title, padding, onHeight) {
    frameCount += 1;
    var id = 'f' + frameCount;
    var frame = el('iframe', {title: title, loading: 'eager'});
    frames[id] = {node: frame, ready: false, pending: null, onHeight: onHeight};
    frame.srcdoc = frameDoc(id, padding);
    return {
      node: frame,
      show: function (html, surface) {
        var message = {html: withAssets(html), surface: surface};
        if (frames[id].ready) frame.contentWindow.postMessage(message, '*');
        else frames[id].pending = message;
      },
    };
  }

  window.addEventListener('message', function (e) {
    var message = e.data;
    if (!message || !message.libFrame) return;
    var entry = frames[message.libFrame];
    if (!entry) return;
    if (message.ready) {
      entry.ready = true;
      if (entry.pending) entry.node.contentWindow.postMessage(entry.pending, '*');
      entry.pending = null;
    }
    if (message.height && entry.onHeight) entry.onHeight(message.height);
    if (message.said && said) said.textContent = message.said;
  });

  function segmented(label, options, current, onPick) {
    var group = el('div', {class: 'lib__seg', role: 'group', 'aria-label': label});
    options.forEach(function (option) {
      group.appendChild(el('button', {
        class: 'btn', type: 'button', text: option, 'aria-pressed': option === current ? 'true' : 'false',
        onclick: function () {
          Array.prototype.forEach.call(group.children, function (b) { b.setAttribute('aria-pressed', 'false'); });
          this.setAttribute('aria-pressed', 'true');
          onPick(option);
        },
      }));
    });
    return group;
  }

  function controlValues(page) {
    var values = {};
    page.controls.forEach(function (c) { values[c.name] = c.default; });
    return values;
  }

  function keyFor(page, values) {
    return page.controls.filter(function (c) { return c.kind !== 'text'; })
      .map(function (c) { return String(values[c.name]); }).join('|');
  }

  function textValues(page, values) {
    var text = {};
    page.controls.forEach(function (c) { if (c.kind === 'text') text[c.name] = values[c.name]; });
    return text;
  }

  function defaultText(page) {
    var text = {};
    page.controls.forEach(function (c) { if (c.kind === 'text') text[c.name] = c.default; });
    return text;
  }

  function controlField(page, control, values, refresh) {
    var id = 'ctl-' + page.id + '-' + control.name;
    if (control.kind === 'toggle') {
      return el('label', {class: 'field--check'}, [
        el('input', {id: id, type: 'checkbox', checked: values[control.name], onchange: function () {
          values[control.name] = this.checked;
          refresh();
        }}),
        el('span', {text: control.label}),
      ]);
    }
    var input;
    if (control.kind === 'choice') {
      input = el('select', {id: id, onchange: function () { values[control.name] = this.value; refresh(); }},
        control.options.map(function (option) { return el('option', {value: option, text: option}); }));
      input.value = String(values[control.name]);
    } else {
      input = el('input', {id: id, type: 'text', oninput: function () { values[control.name] = this.value; refresh(); }});
      input.value = values[control.name];
    }
    return el('div', {class: 'field'}, [el('label', {for: id, text: control.label}), input]);
  }

  function codeBox(text, what) {
    var pre = el('pre', {class: 'lib__code', tabindex: '0'});
    pre.textContent = text;
    return {
      node: el('div', {}, [pre]),
      set: function (next) { pre.textContent = next; text = next; },
      copy: function () { copy(text, what); },
    };
  }

  function renderCode(page, current) {
    var tabs = ['Usage', 'HTML'];
    if (page.macros.length) tabs.push('Macro');
    if (page.css.length) tabs.push('CSS');
    var hasPreview = Object.keys(page.combos).length > 0;
    if (!hasPreview) tabs = tabs.filter(function (t) { return t !== 'Usage' && t !== 'HTML'; });
    if (tabs.indexOf(state.tab) < 0) state.tab = tabs[0];

    function textFor(tab) {
      if (tab === 'Usage') return current().usage;
      if (tab === 'HTML') return current().html;
      if (tab === 'Macro') return page.macros[0].source;
      return page.css.map(function (rule) { return '/* ' + rule.file + ':' + rule.line + ' */\n' + rule.text; }).join('\n\n');
    }

    var code = codeBox(textFor(state.tab), state.tab.toLowerCase());
    var bar = el('div', {class: 'lib__tabs', role: 'tablist'}, tabs.map(function (tab) {
      return el('button', {
        class: 'btn', type: 'button', role: 'tab', text: tab, 'aria-selected': tab === state.tab ? 'true' : 'false',
        onclick: function () {
          state.tab = tab;
          Array.prototype.forEach.call(bar.children, function (b) { b.setAttribute('aria-selected', b.textContent === tab ? 'true' : 'false'); });
          code.set(textFor(tab));
        },
      });
    }));
    var box = el('section', {class: 'lib__box', 'aria-label': 'Code'}, [
      el('div', {class: 'lib__box-head'}, [
        bar,
        el('button', {class: 'btn', type: 'button', text: 'Copy', onclick: function () { code.copy(); }}),
      ]),
      code.node,
    ]);
    return {node: box, refresh: function () { code.set(textFor(state.tab)); }};
  }

  function renderProps(page) {
    var withArgs = page.macros.filter(function (m) { return m.args.length; });
    if (!withArgs.length) return null;
    var rows = [];
    withArgs.forEach(function (macro) {
      macro.args.forEach(function (arg) {
        rows.push(el('tr', {}, [
          el('td', {}, el('code', {text: arg.name})),
          el('td', {class: 'muted', text: withArgs.length > 1 ? macro.name : null}),
          el('td', {}, arg.required ? el('span', {class: 'status-text status-text--warn', text: 'Required'}) : el('code', {text: arg.default})),
        ]));
      });
    });
    return el('section', {class: 'lib__box'}, [
      el('div', {class: 'lib__box-head'}, el('h3', {class: 'lib__box-title', text: 'Arguments'})),
      el('div', {class: 'table-scroll', style: 'margin:0;box-shadow:none'}, el('table', {style: 'border:0'}, [
        el('thead', {}, el('tr', {}, [el('th', {text: 'Name'}), el('th', {text: withArgs.length > 1 ? 'Macro' : ''}), el('th', {text: 'Default'})])),
        el('tbody', {}, rows),
      ])),
    ]);
  }

  function renderUsedIn(page) {
    if (!page.usedIn.length) return null;
    return el('section', {class: 'lib__box'}, [
      el('div', {class: 'lib__box-head'}, el('h3', {class: 'lib__box-title', text: 'Used in'})),
      el('div', {class: 'lib__pad'}, [
        el('ul', {class: 'lib__usedin'}, page.usedIn.map(function (use) {
          return el('li', {}, [el('a', {href: use.url, target: '_blank', rel: 'noopener', text: use.file}), el('span', {text: '×' + use.count})]);
        })),
      ]),
    ]);
  }

  function renderHead(page) {
    var total = page.usedIn.reduce(function (sum, use) { return sum + use.count; }, 0);
    var source = page.macros[0] ? {label: page.macros[0].file, url: page.macros[0].url}
      : page.css[0] ? {label: page.css[0].file + ':' + page.css[0].line, url: page.css[0].url} : null;
    return el('header', {class: 'lib__head'}, [
      el('h2', {text: page.name}),
      el('p', {class: 'lib__summary', text: page.summary}),
      el('div', {class: 'lib__meta'}, [
        el('span', {class: 'badge' + (page.kind === 'macro' ? ' badge--info' : ''), text: page.kind === 'macro' ? 'Jinja macro' : 'CSS classes'}),
        page.group === 'New' ? el('span', {class: 'badge badge--warn', text: 'New'}) : null,
        total ? el('span', {class: 'muted', text: total + (total === 1 ? ' use' : ' uses') + ' in ' + page.usedIn.length + (page.usedIn.length === 1 ? ' file' : ' files')}) : null,
        source ? el('a', {href: source.url, target: '_blank', rel: 'noopener', text: source.label}) : null,
      ]),
    ]);
  }

  function renderComponent(page) {
    var nodes = [renderHead(page)];
    var hasPreview = Object.keys(page.combos).length > 0;
    if (page.previewError) nodes.push(el('div', {class: 'notice notice--warn', text: page.previewError}));

    if (!hasPreview) {
      if (!page.previewError) {
        nodes.push(el('div', {class: 'notice', text: 'These styles have no live preview yet. Add an entry to catalog.py to give them one.'}));
      }
      nodes.push(renderCode(page, null).node);
      var props = renderProps(page);
      if (props) nodes.push(props);
      return nodes;
    }

    var values = controlValues(page);
    var width = page.phone ? 'Phone' : state.width;
    var preview = el('div', {class: 'lib__preview' + (width === 'Phone' ? ' lib__preview--phone' : '')});
    var stage = makeFrame(page.name + ' preview', 24, function (height) {
      stage.node.style.height = Math.max(150, height) + 'px';
    });
    preview.appendChild(stage.node);
    said = el('p', {class: 'lib__said', text: 'Hover, click and tab through the preview. It is the real markup and CSS.'});

    function current() {
      var combo = page.combos[keyFor(page, values)];
      var text = textValues(page, values);
      return {html: fill(combo.html, text, escapeHtml), usage: fill(combo.usage, text, escapeJinja)};
    }

    var code = renderCode(page, current);
    function refresh() {
      stage.show(current().html, SURFACES[state.surface]);
      code.refresh();
    }
    function refreshGallery() {
      gallery.forEach(function (tile) { tile.frame.show(tile.html, SURFACES[state.surface]); });
    }

    var controlsList = page.controls.length
      ? page.controls.map(function (control) { return controlField(page, control, values, refresh); })
      : [el('p', {class: 'lib__no-controls', text: 'This one has nothing to change.'})];

    nodes.push(el('section', {class: 'lib__box', 'aria-label': 'Playground'}, [
      el('div', {class: 'lib__box-head'}, [
        el('h3', {class: 'lib__box-title', text: 'Try it'}),
        el('div', {class: 'lib__options'}, [
          segmented('Preview width', ['Desktop', 'Phone'], width, function (next) {
            state.width = next;
            preview.classList.toggle('lib__preview--phone', next === 'Phone');
          }),
          segmented('Background', Object.keys(SURFACES), state.surface, function (next) {
            state.surface = next;
            refresh();
            refreshGallery();
          }),
        ]),
      ]),
      el('div', {class: 'lib__stage'}, [preview, el('div', {class: 'lib__controls'}, controlsList)]),
      said,
    ]));

    var gallery = page.gallery.map(function (item) {
      var frame = makeFrame(item.label, 10, null);
      return {
        frame: frame,
        html: fill(item.html, defaultText(page), escapeHtml),
        node: el('button', {
          type: 'button', title: 'Show ' + item.label + ' in the preview',
          onclick: function () { applyCase(item.label); },
        }, [frame.node, el('span', {class: 'lib__cap', text: item.label})]),
      };
    });

    function applyCase(label) {
      page.controls.forEach(function (control) {
        if (control.kind === 'choice' && control === firstChoice() && control.options.indexOf(label) >= 0) {
          values[control.name] = label;
        }
        if (control.kind === 'toggle' && label.indexOf(control.label + ':') === 0) {
          values[control.name] = label.slice(-2) === 'on';
        }
        var input = document.getElementById('ctl-' + page.id + '-' + control.name);
        if (input && control.kind === 'toggle') input.checked = values[control.name];
        else if (input) input.value = values[control.name];
      });
      refresh();
    }

    function firstChoice() {
      return page.controls.filter(function (c) { return c.kind === 'choice'; })[0];
    }

    if (gallery.length) {
      nodes.push(el('section', {class: 'lib__box', 'aria-label': 'Variants'}, [
        el('div', {class: 'lib__box-head'}, el('h3', {class: 'lib__box-title', text: 'Variants'})),
        el('div', {class: 'lib__gallery'}, gallery.map(function (tile) { return tile.node; })),
      ]));
    }

    nodes.push(code.node);
    if (page.notes.length) {
      nodes.push(el('section', {class: 'lib__box'}, [
        el('div', {class: 'lib__box-head'}, el('h3', {class: 'lib__box-title', text: 'Notes'})),
        el('div', {class: 'lib__pad'}, el('ul', {class: 'lib__notes'}, page.notes.map(function (note) { return el('li', {text: note}); }))),
      ]));
    }
    var args = renderProps(page);
    if (args) nodes.push(args);
    var uses = renderUsedIn(page);
    if (uses) nodes.push(uses);

    refresh();
    refreshGallery();
    return nodes;
  }

  function tokenButton(token, preview) {
    return el('button', {
      class: 'lib__token', type: 'button', title: 'Copy var(' + token.name + ')',
      onclick: function () { copy('var(' + token.name + ')', token.name); },
    }, [preview, el('code', {text: token.name}), el('span', {class: 'lib__value', text: token.value})]);
  }

  function tokenPreview(page, token) {
    if (page.id === 'colors') return el('span', {class: 'lib__swatch'}, el('i', {style: 'background:var(' + token.name + ')'}));
    if (/^--text-/.test(token.name)) return el('span', {style: 'font-size:var(' + token.name + ')', text: 'Deploy app'});
    if (/^--font-/.test(token.name)) return el('span', {style: 'font-family:var(' + token.name + ')', text: 'files.example.com'});
    if (/^--weight-/.test(token.name)) return el('span', {style: 'font-weight:var(' + token.name + ')', text: 'Deploy app'});
    if (/^--space-/.test(token.name)) return el('span', {class: 'lib__bar', style: 'width:var(' + token.name + ')'});
    if (/^--radius-/.test(token.name)) return el('span', {class: 'lib__bar', style: 'height:32px;width:32px;border-radius:var(' + token.name + ')'});
    if (/^--shadow-/.test(token.name)) return el('span', {class: 'lib__box', style: 'display:block;height:32px'});
    return null;
  }

  function renderFoundation(page) {
    var head = el('header', {class: 'lib__head'}, [
      el('h2', {text: page.name}),
      el('p', {class: 'lib__summary', text: page.id === 'icons'
        ? 'Every file in static/img/icons. Click one to copy its macro call.'
        : 'Read from tokens.css every time the page is built. Click one to copy its var().'}),
      el('div', {class: 'lib__meta'}, el('a', {href: data.tokensUrl, target: '_blank', rel: 'noopener', text: 'tokens.css'})),
    ]);
    if (page.id === 'icons') {
      return [head, el('div', {class: 'lib__tokens'}, data.icons.map(function (name) {
        var call = '{{ icon("' + name + '", 16) }}';
        return el('button', {class: 'lib__token lib__icon', type: 'button', onclick: function () { copy(call, name); }}, [
          el('img', {src: data.assets['/static/img/icons/' + name + '.svg'], alt: ''}),
          el('code', {text: name}),
        ]);
      }))];
    }
    var groups = [];
    data.tokens.filter(function (t) { return page.test.test(t.name); }).forEach(function (token) {
      var title = page.id === 'colors' ? token.group : token.name.split('-')[2];
      var group = groups.filter(function (g) { return g.title === title; })[0];
      if (!group) groups.push(group = {title: title, tokens: []});
      group.tokens.push(token);
    });
    return [head].concat(groups.map(function (group) {
      return el('section', {}, [
        el('h3', {class: 'section-label lib__group-title', text: group.title}),
        el('div', {class: 'lib__tokens'}, group.tokens.map(function (token) { return tokenButton(token, tokenPreview(page, token)); })),
      ]);
    }));
  }

  function allPages() {
    return data.pages.concat(FOUNDATIONS);
  }

  function matches(page, query) {
    if (!query) return true;
    var haystack = [page.name, page.group, page.summary || ''];
    if (page.macros) page.macros.forEach(function (m) { haystack.push(m.name); });
    if (page.css) page.css.forEach(function (rule) { haystack.push(rule.text.split('{')[0]); });
    return haystack.join(' ').toLowerCase().indexOf(query) >= 0;
  }

  function renderNav(activeId) {
    var query = state.query.trim().toLowerCase();
    var groups = [];
    allPages().filter(function (page) { return matches(page, query); }).forEach(function (page) {
      var group = groups.filter(function (g) { return g.name === page.group; })[0];
      if (!group) groups.push(group = {name: page.group, pages: []});
      group.pages.push(page);
    });
    nav.textContent = '';
    if (!groups.length) nav.appendChild(el('p', {class: 'muted', text: 'Nothing matches that. Try a class name such as btn.'}));
    groups.forEach(function (group) {
      nav.appendChild(el('div', {class: 'lib__nav-group', text: group.name}));
      group.pages.forEach(function (page) {
        nav.appendChild(el('a', {href: '#' + page.id, 'aria-current': page.id === activeId ? 'page' : null, onclick: function (e) {
          e.preventDefault();
          open(page.id);
        }}, [el('span', {text: page.name}), page.group === 'New' ? el('span', {class: 'lib__dot', title: 'New'}) : null]));
      });
    });
  }

  function open(id) {
    var page = allPages().filter(function (p) { return p.id === id; })[0] || allPages().filter(function (p) { return p.id === 'button'; })[0];
    frames = {};
    said = null;
    activeId = page.id;
    var nodes = page.group === 'Foundations' ? renderFoundation(page) : renderComponent(page);
    main.textContent = '';
    nodes.forEach(function (node) { main.appendChild(node); });
    renderNav(page.id);
    try { history.replaceState(null, '', '#' + page.id); } catch (e) { /* the viewer may not allow it */ }
    window.scrollTo(0, 0);
  }

  window.addEventListener('hashchange', function () {
    var id = location.hash.slice(1);
    if (id && id !== activeId) open(id);
  });

  filter.addEventListener('input', function () {
    state.query = filter.value;
    renderNav(activeId);
  });

  filter.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var first = nav.querySelector('a');
    if (first) first.click();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
      e.preventDefault();
      filter.focus();
    }
  });

  var repo = data.repo;
  document.getElementById('lib-stamp').textContent = 'Built ' + repo.built + ' from ' + repo.branch + ' at ' + repo.commit +
    (repo.uncommitted ? ', with uncommitted changes' : '');

  open(location.hash.slice(1) || 'button');
})();
