/** Fixed Silverchair DOM parser. Self-contained for browser evaluate serialization. */
export function inspectSilverchairDOM() {
  const wrapped = Array.from(document.querySelectorAll('.al-article-item-wrap'));
  const cards = wrapped.length ? wrapped : Array.from(document.querySelectorAll('.al-article-items'));
  const current = new URL(location.href);
  const identity = current.pathname.match(/^\/(?:aip\/)?([^/]+)\/issues?\/(\d+)\/(\d+)\/?$/);
  const incomplete = [];
  if (identity) {
    const marker = /\b(?:in progress|partial)\b/i;
    for (const option of document.querySelectorAll('option:checked, option[selected], option.selected')) {
      const value=option.getAttribute('value');
      if(!value) continue;
      const target = new URL(value, current);
      const candidate = target.pathname.match(/^\/(?:aip\/)?([^/]+)\/issues?\/(\d+)\/(\d+)\/?$/);
      const text = (option.innerText || option.textContent || '').trim();
      if (target.origin === current.origin && candidate && candidate.slice(1).join('/') === identity.slice(1).join('/') && marker.test(text)) {
        incomplete.push({kind:'selected_option',url:target.href,text});
      }
    }
    const label = new RegExp('\\bVolume\\s+'+identity[2]+'\\s*,?\\s*Issue\\s+'+identity[3]+'(?!\\d)', 'i');
    for (const heading of document.querySelectorAll('h1,h2,.volume-issue__wrap')) {
      const text = (heading.innerText || heading.textContent || '').trim();
      if (label.test(text) && marker.test(text)) incomplete.push({kind:'current_heading',url:current.href,text});
    }
  }
  return {
    issue_completion_markers: incomplete,
    url: location.href,
    headings: Array.from(document.querySelectorAll('h2, .volume-issue__wrap')).map(e => e.innerText.trim()),
    cards: cards.map(card => {
      const text = selector => card.querySelector(selector)?.innerText?.replace(/\s+/g, ' ').trim() || '';
      const controls = Array.from(card.querySelectorAll('.showAbstractLink'));
      const controlKind = control => (control?.getAttribute('data-abstract-type') || control?.innerText || '').trim().toLowerCase();
      const control = controls.find(e => controlKind(e) === 'abstract') ||
        controls.find(e => controlKind(e) === 'extract') || controls[0];
      const kind = controlKind(control) || 'unavailable';
      // The control ID is opaque: NEVER derive the panel ID by string replacement.
      const panelId = control?.getAttribute('aria-controls') || '';
      const panel = panelId ? document.getElementById(panelId) : null;
      let abstract = '';
      if (panel?.getAttribute('aria-hidden') === 'false') {
        if (kind === 'extract') {
          // Publisher Extracts also use block-child-p for display formulas.
          // Select only this control's panel; retain outermost reading blocks
          // so nested paragraphs are included once without losing surrounding text.
          const blocks = Array.from(panel.querySelectorAll('p, div.block-child-p'));
          abstract = blocks.filter(e => !blocks.some(parent => parent !== e && parent.contains(e))).map(e => {
            return e.innerHTML.replace(/<sup[^>]*>(.*?)<\/sup>/gi, '^{$1}')
              .replace(/<sub[^>]*>(.*?)<\/sub>/gi, '_{$1}')
              .replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
              .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
              .replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();
          }).filter(Boolean).join('\n\n');
        } else {
          const main = panel.querySelector('section.abstract[aria-label="Main abstract"]');
          abstract = main ? main.innerText.trim() : Array.from(
            panel.querySelectorAll('section.abstract:not(.graphicalAbstract)')
          ).map(e => e.innerText.trim()).join(' ');
        }
      }
      return {
        title: text('h3.item-title a'), article_url: card.querySelector('h3.item-title a')?.getAttribute('href') || '',
        doi: card.querySelector('.citation-label a')?.getAttribute('href') || '',
        authors: Array.from(card.querySelectorAll('.wi-fullname')).map(e => e.innerText.trim()),
        citation: text('.ww-citation-primary'), abstract, abstract_type: kind,
        unavailable_reason: !control ? 'No Abstract or Extract control on the official issue card' : '',
        control: control?.id || '', panel: panelId,
        expanded: control?.getAttribute('aria-expanded') === 'true',
      };
    }),
  };
}
