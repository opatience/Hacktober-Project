(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    preferences: { preference_text: '', interest_tag_ids: [], excluded_tag_ids: [], followed_only: false, free_only: false, horizon_days: 30, version: 1 },
    follows: [], tags: [], events: [], posts: [], query: '', calendar: null, sourceMode: null,
  };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
  const icons = {
    clock: '<svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg>',
    pin: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.5"/></svg>',
    ticket: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M3 7h18v4a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4V7Z"/><path d="M15 7v2m0 3v2m0 2v2"/></svg>',
    remove: '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m6 6 12 12M18 6 6 18"/></svg>',
  };

  function safeUrl(value) {
    if (!value) return '';
    try {
      const url = new URL(value, window.location.origin);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : '';
    } catch { return ''; }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: 'same-origin', ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
    if (response.status === 401) { window.location.href = '/login'; throw new Error('Log in to continue.'); }
    if (response.status === 204) return {};
    let data;
    try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Try again.'); }
    if (!response.ok) {
      const errorText = typeof data.error === 'string' ? data.error : data.error?.message || data.message;
      throw new Error(errorText || `This request could not finish (${response.status}). Try again.`);
    }
    return data;
  }

  function message(text, kind = 'status') {
    const target = $(kind);
    target.textContent = text || '';
    target.hidden = !text;
  }

  async function withButton(button, loadingText, action) {
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = loadingText;
    message('', 'error');
    try { await action(); } catch (error) { message(error.message, 'error'); }
    finally { button.disabled = false; button.innerHTML = original; }
  }

  function sourceUrl(item) {
    if (['fixture', 'mock'].includes(item.source_mode || state.sourceMode)) return '';
    return safeUrl(item.url || item.permalink || item.source_posts?.find((source) => source.is_primary)?.url || item.source_posts?.[0]?.url);
  }

  function sourceLink(item, text = 'View original post') {
    const url = sourceUrl(item);
    return url ? `<a class="source-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : '';
  }

  function emptyState(title, description) {
    return `<div class="empty-state"><h3>${esc(title)}</h3><p>${esc(description)}</p></div>`;
  }

  function updateMeta(meta = {}, warnings = []) {
    if (meta.source_mode) state.sourceMode = meta.source_mode;
    const allWarnings = [...(Array.isArray(meta.warnings) ? meta.warnings : []), ...(Array.isArray(warnings) ? warnings : [])];
    if (meta.search_degraded && !allWarnings.some((warning) => /search/i.test(String(warning)))) allWarnings.push('Search is temporarily unavailable. Events are ranked using your follows and interests.');
    message([...new Set(allWarnings.map((warning) => typeof warning === 'string' ? warning : warning.message || 'Some source information is unavailable.'))].join(' '), 'warnings');
    if (meta.generated_at) {
      const date = new Date(meta.generated_at);
      if (!Number.isNaN(date.getTime())) $('feed-updated').textContent = `Updated ${new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(date)}`;
    }
  }

  function renderFollows() {
    $('follow-count').textContent = state.follows.length;
    $('following-list').innerHTML = state.follows.length ? state.follows.map((follow) => {
      const username = follow.username || follow.account?.username || '';
      const accountId = follow.account_id || follow.account?.account_id || '';
      const status = { pending: 'Ready to refresh', queued: 'Waiting for posts', failed: 'Posts unavailable — try refreshing', private: 'Private account', blocked: 'Account unavailable' }[follow.ingestion_status] || '';
      return `<li class="following-item"><div><span class="follow-name">@${esc(username)}</span>${status ? `<span class="follow-state">${esc(status)}</span>` : ''}</div><button class="remove-follow" type="button" data-account-id="${esc(accountId)}" aria-label="Unfollow ${esc(username)}" title="Unfollow ${esc(username)}">${icons.remove}</button></li>`;
    }).join('') : '<li class="help-text">Add a club handle below, then refresh posts.</li>';
    $('refresh-button').disabled = !state.follows.length;
    $('refresh-button').title = state.follows.length ? '' : 'Follow an account first';
  }

  function renderTags() {
    const render = (kind) => state.tags.map((tag) => {
      const id = tag.tag_id || tag.id;
      const selected = (state.preferences[kind] || []).includes(id);
      return `<label class="tag-option"><input type="checkbox" name="${esc(kind)}" value="${esc(id)}"${selected ? ' checked' : ''}><span>${esc(tag.label || tag.name || tag.slug)}</span></label>`;
    }).join('');
    $('interest-tags').innerHTML = render('interest_tag_ids') || '<span class="help-text">Add your interests in the text above.</span>';
    $('excluded-tags').innerHTML = render('excluded_tag_ids') || '<span class="help-text">No topics are available yet.</span>';
  }

  function renderPreferences() {
    const preferences = state.preferences;
    $('preference-text').value = preferences.preference_text || '';
    $('free-only').checked = Boolean(preferences.free_only);
    $('followed-only').checked = Boolean(preferences.followed_only);
    $('horizon-days').value = preferences.horizon_days || 30;
    renderTags();
  }

  function eventDate(event) {
    const precision = event.date_precision || (String(event.start || '').length === 10 ? 'date' : 'exact');
    const localDate = event.local_start_date || String(event.start_at || event.start || '').slice(0, 10);
    const hasExactTime = ['exact', 'datetime'].includes(precision) && (event.start_at || event.start) && String(event.start_at || event.start).includes('T');
    const calendarDate = /^\d{4}-\d{2}-\d{2}$/.test(localDate) ? new Date(`${localDate}T12:00:00Z`) : null;
    let timeText = 'Time not provided';
    if (hasExactTime) {
      const value = new Date(event.start_at || event.start);
      if (!Number.isNaN(value.getTime())) {
        const options = { hour: 'numeric', minute: '2-digit', timeZone: event.event_timezone || 'America/Indiana/Indianapolis' };
        try { timeText = new Intl.DateTimeFormat('en-US', options).format(value); }
        catch { timeText = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit' }).format(value); }
      }
    }
    if (!calendarDate || Number.isNaN(calendarDate.getTime())) return { month: 'Date', day: '—', weekday: 'not provided', timeText, localDate: null, hasExactTime: false };
    const format = (options) => new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' }).format(calendarDate);
    return { month: format({ month: 'short' }), day: format({ day: 'numeric' }), weekday: format({ weekday: 'short' }), timeText, localDate, hasExactTime };
  }

  function admission(event) {
    if (event.is_free === true) return '<span class="free-label">Free admission</span>';
    if (event.price_amount !== null && event.price_amount !== undefined && event.price_amount !== '') {
      const amount = Number(event.price_amount);
      if (Number.isFinite(amount)) {
        try { return esc(new Intl.NumberFormat('en-US', { style: 'currency', currency: event.price_currency || 'USD' }).format(amount)); }
        catch { return `${esc(event.price_amount)} ${esc(event.price_currency || 'USD')}`; }
      }
    }
    return 'Cost not provided';
  }

  function renderEvents() {
    $('event-count').textContent = state.events.length;
    const horizon = state.preferences.horizon_days || 30;
    $('feed-description').textContent = state.query ? `Results for “${state.query}” · next ${horizon} days` : `Picked for you · next ${horizon} days`;
    $('event-list').innerHTML = state.events.length ? state.events.map((event) => {
      const date = eventDate(event);
      const club = event.club_name || event.club?.name || (event.username ? `@${event.username}` : 'Campus event');
      const tags = (event.tags || []).map((tag) => typeof tag === 'string' ? tag : tag.label || tag.name).filter(Boolean);
      const reasonLabels = { followed_account: 'From an account you follow', interest_tag: 'Matches your interests', semantic_match: 'Matches your preferences', keyword_match: 'Relevant to your request', text_match: 'Relevant to your request', preference_match: 'Matches your preferences', preferred_date: 'Matches your preferred dates', preferred_price: 'Matches your price preference' };
      const reasons = [...new Set((event.reasons || []).map((reason) => reasonLabels[reason]).filter(Boolean))];
      return `<article class="event-row"><div class="date-stamp" aria-label="${esc(date.localDate || 'Date not provided')}"><span class="date-month">${esc(date.month)}</span><span class="date-day">${esc(date.day)}</span><span class="date-weekday">${esc(date.weekday)}</span></div><div><span class="event-club">${esc(club)}</span><h3>${esc(event.title || 'Upcoming club event')}</h3>${event.description ? `<p class="event-description">${esc(event.description)}</p>` : ''}<div class="event-details"><span class="event-detail">${icons.clock}${esc(date.timeText)}</span><span class="event-detail">${icons.pin}${esc(event.location_name || event.location || 'Location not provided')}</span><span class="event-detail">${icons.ticket}${admission(event)}</span></div>${reasons.length ? `<p class="event-reasons">${esc(reasons.join(' · '))}</p>` : ''}<div class="event-bottom"><div class="event-tags">${tags.slice(0, 5).map((tag) => `<span class="event-tag">${esc(tag)}</span>`).join('')}</div>${sourceLink(event)}</div></div></article>`;
    }).join('') : emptyState(
      state.query || state.preferences.free_only || state.preferences.followed_only ? 'No events match these preferences yet.' : 'Your next campus moment starts here.',
      state.follows.length ? 'Refresh posts to check your followed accounts. You can also adjust your interests or date range.' : 'Follow a club’s Instagram account, then refresh posts to find upcoming events.'
    );
    renderCalendar();
  }

  function renderNewsletter(summary) {
    $('newsletter-summary').textContent = summary || '';
    $('newsletter-summary').hidden = !summary;
    $('newsletter').innerHTML = state.posts.length ? state.posts.map((post) => {
      const username = post.username || post.account?.username || 'Club update';
      const image = safeUrl(post.image || post.media_urls?.[0]);
      let date = '';
      if (post.timestamp || post.posted_at) {
        const parsed = new Date(post.timestamp || post.posted_at);
        if (!Number.isNaN(parsed.getTime())) date = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' }).format(parsed);
      }
      return `<article class="newsletter-post">${image && !['fixture', 'mock'].includes(post.source_mode || state.sourceMode) ? `<img class="post-image" src="${esc(image)}" loading="lazy" referrerpolicy="no-referrer" alt="" onerror="this.hidden=true">` : ''}<div class="post-content"><div class="post-heading"><span class="post-username">@${esc(username)}</span>${date ? `<span class="post-date">${esc(date)}</span>` : ''}</div><p class="post-summary">${esc(post.summary || post.caption || 'A new post from this account.')}</p>${sourceLink(post)}</div></article>`;
    }).join('') : emptyState('A little less scrolling.', state.follows.length ? 'Refresh posts to bring recent updates from the accounts you follow into one place.' : 'Follow an account to start your newsletter. Club updates will appear here after you refresh posts.');
  }

  function renderCalendar() {
    if (!window.FullCalendar) { $('calendar').hidden = true; $('calendar-fallback').hidden = false; return; }
    if (!state.calendar) {
      state.calendar = new window.FullCalendar.Calendar($('calendar'), {
        initialView: 'dayGridMonth', height: 'auto', firstDay: 0,
        headerToolbar: { left: 'prev,next today', center: 'title', right: '' }, buttonText: { today: 'Today' },
        eventClick(info) { const url = safeUrl(info.event.url); info.jsEvent.preventDefault(); if (url) window.open(url, '_blank', 'noopener,noreferrer'); },
      });
      state.calendar.render();
    }
    state.calendar.removeAllEvents();
    (state.calendarEvents || state.events).forEach((event) => {
      const date = eventDate(event);
      if (!date.localDate) return;
      let start = date.localDate;
      if (date.hasExactTime) {
        const parsed = new Date(event.start_at || event.start);
        if (!Number.isNaN(parsed.getTime())) {
          try {
            const parts = new Intl.DateTimeFormat('en-CA', { timeZone: event.event_timezone || 'America/Indiana/Indianapolis', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(parsed);
            const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
            start = `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:00`;
          } catch { start = date.localDate; }
        }
      }
      state.calendar.addEvent({ id: event.event_id, title: event.title || 'Club event', start, allDay: !date.hasExactTime, url: sourceUrl(event), color: event.is_past ? '#b8b2c4' : undefined });
    });
  }

  async function loadFollows() {
    const data = await api('/v1/me/follows');
    state.follows = data.follows || [];
    renderFollows();
  }

  async function loadEvents() {
    const params = new URLSearchParams({ horizon_days: state.preferences.horizon_days || 30, free_only: Boolean(state.preferences.free_only), followed_only: Boolean(state.preferences.followed_only) });
    if (state.query) params.set('q', state.query);
    const shift = (days) => { const d = new Date(); d.setDate(d.getDate() + days); return d.toLocaleDateString('en-CA'); };
    const calendarParams = new URLSearchParams({ include_past: true, from_date: shift(-45), through_date: shift(45), limit: 50 });
    const [data, calendarData] = await Promise.all([api(`/v1/events?${params}`), api(`/v1/events?${calendarParams}`)]);
    state.events = data.events || [];
    state.calendarEvents = calendarData.events || [];
    updateMeta(data.meta, data.warnings);
    renderEvents();
  }

  async function loadNewsletter() {
    const data = await api('/v1/newsletter');
    state.posts = data.posts || [];
    if (data.meta?.source_mode) state.sourceMode = data.meta.source_mode;
    renderNewsletter(data.summary);
  }

  async function refreshFeeds() {
    const results = await Promise.allSettled([loadEvents(), loadNewsletter()]);
    const failures = results.filter((result) => result.status === 'rejected');
    if (failures.length) throw failures[0].reason;
  }

  $('follow-form').addEventListener('submit', (event) => {
    event.preventDefault();
    withButton($('follow-button'), 'Adding…', async () => {
      await api('/v1/me/follows', { method: 'POST', body: JSON.stringify({ username: $('username').value.trim() }) });
      $('username').value = '';
      await loadFollows();
      await refreshFeeds();
      message('Account followed. Refresh posts to collect its latest updates.');
    });
  });

  $('following-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-account-id]');
    if (!button) return;
    withButton(button, '…', async () => {
      await api(`/v1/me/follows/${encodeURIComponent(button.dataset.accountId)}`, { method: 'DELETE' });
      await loadFollows();
      await refreshFeeds();
      message('Account unfollowed. Your feed has been updated.');
    });
  });

  $('preferences-form').addEventListener('submit', (event) => {
    event.preventDefault();
    withButton($('save-preferences'), 'Saving…', async () => {
      const selected = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
      const interestIds = selected('interest_tag_ids');
      const excludedIds = selected('excluded_tag_ids');
      if (interestIds.some((id) => excludedIds.includes(id))) throw new Error('A topic cannot be both an interest and a topic to avoid. Uncheck it in one list.');
      const preferences = { preference_text: $('preference-text').value, interest_tag_ids: interestIds, excluded_tag_ids: excludedIds, free_only: $('free-only').checked, followed_only: $('followed-only').checked, horizon_days: Number($('horizon-days').value), expected_version: state.preferences.version };
      const data = await api('/v1/me/preferences', { method: 'PUT', body: JSON.stringify(preferences) });
      state.preferences = { ...state.preferences, ...data.preferences };
      renderPreferences();
      await loadEvents();
      message('Preferences saved. Your upcoming events are updated.');
    });
  });

  $('search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    withButton($('search-button'), 'Finding…', async () => {
      state.query = $('event-query').value.trim();
      await loadEvents();
      message(state.events.length ? `${state.events.length} upcoming ${state.events.length === 1 ? 'event' : 'events'} found.` : 'No matching events found. Adjust your search or preferences.');
    });
  });

  $('event-query').addEventListener('search', () => { if (!$('event-query').value) $('search-form').requestSubmit(); });

  $('refresh-button').addEventListener('click', () => {
    withButton($('refresh-button'), 'Collecting posts…', async () => {
      message('Collecting recent posts from your followed accounts. This can take up to a minute.');
      const data = await api('/v1/refresh', { method: 'POST', body: JSON.stringify({}) });
      await loadFollows();
      await refreshFeeds();
      updateMeta(data.meta, data.warnings);
      message(`${state.posts.length} ${state.posts.length === 1 ? 'post' : 'posts'} in your newsletter · ${state.events.length} upcoming ${state.events.length === 1 ? 'event' : 'events'}.`);
    });
  });

  const tabs = [...document.querySelectorAll('[data-view]')];
  function activateTab(tab, focus = false) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', String(selected));
      item.tabIndex = selected ? 0 : -1;
      $(`${item.dataset.view}-panel`).hidden = !selected;
    });
    if (focus) tab.focus();
    if (tab.dataset.view === 'calendar' && state.calendar) state.calendar.updateSize();
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activateTab(tab));
    tab.addEventListener('keydown', (event) => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); activateTab(tabs[next], true); }
    });
  });

  async function init() {
    try {
      const identity = await api('/v1/me');
      if (identity.meta) updateMeta(identity.meta);
    } catch (error) {
      message(error.message, 'error');
      message('Your session could not start. Refresh the page to try again.');
      $('event-list').innerHTML = emptyState('Your feed could not load.', 'Try refreshing the page after the connection is available.');
      $('newsletter').innerHTML = emptyState('Posts could not load.', 'Try refreshing the page after the connection is available.');
      $('refresh-button').disabled = true;
      $('following-list').innerHTML = '<li class="help-text">Accounts could not load.</li>';
      return;
    }
    const setup = await Promise.allSettled([api('/v1/tags'), api('/v1/me/preferences'), loadFollows()]);
    const [tags, preferences] = setup;
    if (tags.status === 'fulfilled') state.tags = tags.value.tags || [];
    if (preferences.status === 'fulfilled') state.preferences = { ...state.preferences, ...preferences.value.preferences };
    renderPreferences();
    const failures = setup.filter((result) => result.status === 'rejected');
    try {
      await refreshFeeds();
      if (failures.length) throw failures[0].reason;
      message(state.follows.length ? 'Your saved follows and preferences are ready.' : 'Start with one club: follow an account, then refresh its posts.');
    } catch (error) {
      message(error.message, 'error');
      message('Some information could not load. Refresh the page or try again.');
      if (!state.events.length) $('event-list').innerHTML = emptyState('Your feed could not load.', 'Try refreshing the page. Your saved preferences will be kept.');
      if (!state.posts.length) $('newsletter').innerHTML = emptyState('Posts could not load.', 'Try again after the connection is available.');
    }
  }

  init();
})();
