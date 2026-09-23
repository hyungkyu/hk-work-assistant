    const state = { csrf: null, setupRequired: false, settings: {}, secrets: {} };
    const $ = (id) => document.getElementById(id);

    function toast(message, error = false) {
      const node = $('toast');
      node.textContent = message;
      node.className = `toast show${error ? ' error' : ''}`;
      setTimeout(() => { node.className = 'toast'; }, 2600);
    }

    async function api(path, options = {}, csrfRetried = false) {
      const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
      if (state.csrf && options.method && !['GET', 'HEAD'].includes(options.method)) headers['X-CSRF-Token'] = state.csrf;
      const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
      let body = {};
      try { body = await response.json(); } catch (_) {}
      if (!csrfRetried && response.status === 403 && body.detail === 'invalid CSRF token') {
        const sessionResponse = await fetch('/api/v1/admin/session', { credentials: 'same-origin' });
        const session = await sessionResponse.json();
        if (session.authorized && session.csrf_token) {
          state.csrf = session.csrf_token;
          return api(path, options, true);
        }
      }
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      return body;
    }

    function showAuth(session) {
      state.setupRequired = session.setup_required;
      $('auth-overlay').classList.remove('hidden');
      $('auth-title').textContent = session.setup_required ? '비상 슈퍼 관리자 만들기' : '백오피스 로그인';
      $('auth-description').textContent = session.setup_required
        ? '이 PC에서 처음 사용할 관리자 비밀번호를 만드세요. 12자 이상이어야 합니다.'
        : (session.authenticated && !session.authorized ? '현재 회사 계정에는 백오피스 권한이 없습니다.' : '슈퍼 관리자 회사 계정으로 로그인하세요.');
      $('google-login').classList.toggle('hidden', !session.google_login_available);
      $('emergency-area').classList.toggle('hidden', !session.emergency_login_available);
      $('confirm-field').classList.toggle('hidden', !session.setup_required);
      $('auth-submit').textContent = session.setup_required ? '비상 관리자 생성' : '비상 로그인';
      $('admin-password').autocomplete = session.setup_required ? 'new-password' : 'current-password';
      if (!session.google_login_available && !session.emergency_login_available) {
        $('auth-notice').textContent = 'Google OAuth 설정은 이 PC의 로컬 백오피스에서 먼저 구성해야 합니다.';
      }
    }

    async function initialize() {
      for (let hour = 0; hour < 24; hour++) {
        const option = document.createElement('option');
        option.value = String(hour); option.textContent = `${String(hour).padStart(2, '0')}:00`;
        $('daily-hour').appendChild(option);
      }
      try {
        const session = await api('/api/v1/admin/session');
        if (!session.authorized) return showAuth(session);
        state.csrf = session.csrf_token;
        $('auth-overlay').classList.add('hidden');
        await loadSettings();
        // The hash router decides which screen to open and which loader to
        // run, so a shared link, a reload and a first visit all agree.
        applyHash();
      } catch (error) { showAuth({setup_required:false,google_login_available:false,emergency_login_available:false}); $('auth-notice').textContent = error.message; }
    }

    $('auth-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const password = $('admin-password').value;
      if (state.setupRequired && password !== $('admin-password-confirm').value) {
        $('auth-notice').textContent = '비밀번호가 일치하지 않습니다.'; return;
      }
      try {
        await api(state.setupRequired ? '/api/v1/admin/bootstrap' : '/api/v1/admin/login', { method: 'POST', body: JSON.stringify({ password }) });
        const session = await api('/api/v1/admin/session');
        if (!session.authorized) return showAuth(session);
        state.csrf = session.csrf_token;
        $('auth-overlay').classList.add('hidden');
        $('admin-password').value = ''; $('admin-password-confirm').value = ''; $('auth-notice').textContent = '';
        await loadSettings();
        // The hash router decides which screen to open and which loader to
        // run, so a shared link, a reload and a first visit all agree.
        applyHash();
      } catch (error) { $('auth-notice').textContent = error.message; }
    });

    async function loadSettings() {
      const body = await api('/api/v1/admin/settings');
      state.settings = body.settings; state.secrets = body.secrets;
      renderSettings();
      await loadNotionSeeds();
      await loadAudit();
    }

    function renderSettings() {
      const s = state.settings;
      $('company-domain').value = s.company_google_domain || '';
      $('super-admin-email').value = s.super_admin_google_email || '';
      $('oauth-base-url').value = s.google_oauth_base_url || '';
      $('timezone').value = s.timezone;
      $('daily-hour').value = String(s.daily_collection_hour);
      $('data-root').value = s.data_root;
      $('runtime-root').value = s.runtime_root;
      $('drive-id').value = s.google_drive_backup_folder_id || '';
      $('drive-url').value = s.google_drive_backup_folder_url || '';
      $('drive-enabled').checked = Boolean(s.google_drive_backup_enabled);
      $('slack-team').value = s.slack_expected_team_id || '';
      $('github-organization').value = s.github_organization || 'RLWRLD';
      $('model-provider').value = s.local_model_provider;
      $('model-name').value = s.local_model_name || '';
      $('model-endpoint').value = s.local_model_endpoint || '';
      $('metric-data-root').textContent = s.data_root;
      $('metric-drive').textContent = s.google_drive_backup_enabled ? '활성' : (s.google_drive_backup_folder_id ? '준비 중' : '미설정');
      $('metric-slack').textContent = state.secrets.slack_token ? '인증정보 저장됨' : '미설정';
      $('google-client-state').textContent = state.secrets.google_oauth_client ? 'OAuth Client 저장됨' : 'OAuth Client 미설정';
      $('authorize-google-data').classList.toggle('hidden', !state.secrets.google_oauth_client);
      setBadge('google-data-badge', state.secrets.google_token, 'Calendar·Drive 권한 승인됨', 'Calendar·Drive 권한 미승인');
      setBadge('slack-secret-badge', state.secrets.slack_token, '토큰 저장됨', '토큰 미설정');
      setBadge('notion-secret-badge', state.secrets.notion_token, '토큰 저장됨', '토큰 미설정');
      setBadge('github-secret-badge', state.secrets.github_token, '토큰 저장됨', '토큰 미설정');
    }

    function setBadge(id, configured, yes, no) {
      const badge = $(id); badge.textContent = configured ? yes : no; badge.className = configured ? 'badge ok' : 'badge wait';
    }

    function collectSettings() {
      return {
        company_google_domain: $('company-domain').value.trim(),
        super_admin_google_email: $('super-admin-email').value.trim(),
        google_oauth_base_url: $('oauth-base-url').value.trim(),
        timezone: $('timezone').value.trim(),
        daily_collection_hour: Number($('daily-hour').value),
        data_root: $('data-root').value.trim(),
        runtime_root: $('runtime-root').value.trim(),
        google_drive_backup_folder_id: $('drive-id').value.trim(),
        google_drive_backup_folder_url: $('drive-url').value.trim(),
        google_drive_backup_enabled: $('drive-enabled').checked,
        slack_expected_team_id: $('slack-team').value.trim(),
        github_organization: $('github-organization').value.trim(),
        local_model_provider: $('model-provider').value,
        local_model_name: $('model-name').value.trim(),
        local_model_endpoint: $('model-endpoint').value.trim(),
      };
    }

    async function saveSettings() {
      try {
        const body = await api('/api/v1/admin/settings', { method: 'PUT', body: JSON.stringify(collectSettings()) });
        state.settings = body.settings; state.secrets = body.secrets; renderSettings(); await loadAudit(); toast('설정을 저장했습니다.');
      } catch (error) { toast(error.message, true); }
    }

    document.querySelectorAll('.save-settings').forEach((button) => button.addEventListener('click', saveSettings));

    const secretSaveControls = [
      ['google-client', 'save-google-secret'],
      ['slack-token', 'save-slack-secret'],
      ['notion-token', 'save-notion-secret'],
      ['github-token', 'save-github-secret'],
    ];
    function syncSecretSaveButton(inputId, buttonId) {
      $(buttonId).disabled = !$(inputId).value.trim();
    }
    secretSaveControls.forEach(([inputId, buttonId]) => {
      $(inputId).addEventListener('input', () => syncSecretSaveButton(inputId, buttonId));
      syncSecretSaveButton(inputId, buttonId);
    });

    $('save-google-secret').addEventListener('click', async () => {
      const value = $('google-client').value.trim();
      if (!value) return toast('OAuth Client JSON을 입력하세요.', true);
      try {
        await api('/api/v1/admin/secrets/google_oauth_client', { method: 'PUT', body: JSON.stringify({ value }) });
        $('google-client').value = ''; syncSecretSaveButton('google-client', 'save-google-secret'); state.secrets.google_oauth_client = true; renderSettings(); await loadAudit(); toast('Google 인증정보를 저장했습니다.');
      } catch (error) { toast(error.message, true); }
    });

    $('save-slack-secret').addEventListener('click', async () => {
      const value = $('slack-token').value.trim();
      if (!value) return toast('Slack token을 입력하세요.', true);
      try {
        await api('/api/v1/admin/secrets/slack_token', { method: 'PUT', body: JSON.stringify({ value }) });
        $('slack-token').value = ''; syncSecretSaveButton('slack-token', 'save-slack-secret'); state.secrets.slack_token = true; renderSettings(); await saveSettings(); toast('Slack 인증정보를 저장했습니다. 연결 시험을 실행하세요.');
      } catch (error) { toast(error.message, true); }
    });

    async function saveToken(name, inputId, label) {
      const value = $(inputId).value.trim();
      if (!value) return toast(`${label} 토큰을 입력하세요.`, true);
      try {
        await api(`/api/v1/admin/secrets/${name}_token`, { method: 'PUT', body: JSON.stringify({ value }) });
        $(inputId).value = ''; syncSecretSaveButton(inputId, `save-${name}-secret`); state.secrets[`${name}_token`] = true; renderSettings(); await loadAudit();
        toast(`${label} 토큰을 저장했습니다. 연결 시험을 실행하세요.`);
      } catch (error) { toast(error.message, true); }
    }

    $('save-notion-secret').addEventListener('click', () => saveToken('notion', 'notion-token', 'Notion'));
    $('notion-seed-add').addEventListener('click', addNotionSeed);
    $('notion-seed-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') addNotionSeed(); });
    $('save-github-secret').addEventListener('click', async () => { await saveSettings(); await saveToken('github', 'github-token', 'GitHub'); });

    function summarizeConnection(name, result) {
      const lines = [result.ok ? '유효한 연결입니다.' : '연결되었지만 필수 권한이 부족합니다.'];
      if (result.identity) lines.push(`계정: ${result.identity}`);
      if (result.workspace) lines.push(`Workspace: ${result.workspace}`);
      if (result.organization) lines.push(`조직: ${result.organization}`);
      if (result.team_id) lines.push(`Team ID: ${result.team_id}`);
      if (result.has_accessible_content !== undefined) lines.push(`공유 콘텐츠 발견: ${result.has_accessible_content ? '예' : '아니오'}`);
      if (result.repository_access !== undefined) lines.push(`저장소 접근: ${result.repository_access ? '예' : '아니오'}`);
      if (result.missing_scopes?.length) lines.push(`부족한 권한: ${result.missing_scopes.join(', ')}`);
      if (result.note) lines.push(result.note);
      return lines.join('\n');
    }

    async function testConnection(name) {
      const node = $(`result-${name}`); node.textContent = 'API 연결을 확인하고 있습니다…'; node.className = 'result';
      try {
        const body = await api(`/api/v1/admin/connections/${name}/test`, { method: 'POST', body: '{}' });
        node.textContent = summarizeConnection(name, body.result);
        node.className = body.result.ok ? 'result ok' : 'result error';
        await loadAudit();
      } catch (error) { node.textContent = error.message; node.className = 'result error'; }
    }

    ['google', 'slack', 'notion', 'github'].forEach((name) => {
      $(`test-${name}`).addEventListener('click', () => testConnection(name));
    });

    // Notion seed pages. The list is the operator's answer to "and check this
    // one whatever search says", so it is edited where the Notion token is
    // configured rather than on a page of its own.
    async function loadNotionSeeds() {
      const table = $('notion-seed-body');
      const state_line = $('notion-seed-state');
      try {
        const body = await api('/api/v1/admin/notion-seeds');
        table.textContent = '';
        if (!body.seeds.length) {
          state_line.textContent = '등록된 시드가 없습니다. 검색 결과만으로 하루치를 모읍니다.';
          return;
        }
        state_line.textContent = `${body.seeds.length}개. 매 실행 이만큼의 요청이 더 듭니다.`;
        body.seeds.forEach((seed) => {
          const row = document.createElement('tr');
          [seed.page_id, seed.label || '-', seed.added_at ? new Date(seed.added_at).toLocaleDateString() : '-']
            .forEach((value) => { const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell); });
          const actions = document.createElement('td');
          const remove = document.createElement('button');
          remove.className = 'button'; remove.textContent = '삭제';
          remove.addEventListener('click', async () => {
            remove.disabled = true;
            try {
              await api(`/api/v1/admin/notion-seeds/${encodeURIComponent(seed.page_id)}`, {method: 'DELETE'});
              await loadNotionSeeds();
            } catch (error) { state_line.textContent = error.message; remove.disabled = false; }
          });
          actions.appendChild(remove); row.appendChild(actions);
          table.appendChild(row);
        });
      } catch (error) { state_line.textContent = error.message; }
    }

    async function addNotionSeed() {
      const input = $('notion-seed-input');
      const label = $('notion-seed-label');
      const state_line = $('notion-seed-state');
      if (!input.value.trim()) { state_line.textContent = 'Notion 페이지 URL 또는 ID가 필요합니다.'; return; }
      try {
        await api('/api/v1/admin/notion-seeds', {
          method: 'POST',
          body: JSON.stringify({value: input.value, label: label.value}),
        });
        input.value = ''; label.value = '';
        await loadNotionSeeds();
      } catch (error) { state_line.textContent = error.message; }
    }

    // 검색. Read-only, and every hit carries its provenance -- the table shows
    // source and kind next to the snippet so a reader can tell a Slack message
    // from a Notion page without opening anything.
    async function loadSearchCorpus() {
      try {
        const body = await api('/api/v1/admin/collection/search/status');
        const badge = $('search-corpus-badge');
        badge.textContent = `문서 ${body.documents.toLocaleString()}건`;
        badge.className = body.documents ? 'badge ok' : 'badge wait';
      } catch (error) {
        const badge = $('search-corpus-badge');
        badge.textContent = '집계 실패'; badge.className = 'badge wait';
      }
    }

    async function runSearch() {
      const query = $('search-q').value.trim();
      const line = $('search-state');
      const table = $('search-body');
      if (!query) { line.textContent = '찾을 말을 넣으세요.'; return; }
      line.textContent = '찾는 중…';
      const parameters = new URLSearchParams({q: query, matcher: $('search-matcher').value, limit: '50'});
      const source = $('search-source').value;
      if (source) parameters.append('source', source);
      try {
        const body = await api(`/api/v1/admin/collection/search?${parameters}`);
        table.textContent = '';
        $('search-count-badge').textContent = `${body.count}건`;
        // "아무것도 없다"와 "느슨한 쪽으로 내려가서 찾았다"는 다른 답이다.
        const how = body.fell_back
          ? '낱말로는 없어서 부분일치로 찾았습니다'
          : (body.matcher === 'substring' ? '부분일치' : '낱말 일치');
        line.textContent = body.count
          ? `${how} · ${body.took_ms}ms`
          : `찾은 것이 없습니다 (${how} 기준). 부분일치로 바꿔 보세요.`;
        body.hits.forEach((hit) => {
          const row = document.createElement('tr');
          const ref = hit.source_ref && Object.keys(hit.source_ref).length
            ? JSON.stringify(hit.source_ref).slice(0, 60) : '';
          [hit.source, hit.kind, hit.snippet, hit.matcher,
           hit.inserted_at ? new Date(hit.inserted_at).toLocaleDateString() : '-'
          ].forEach((value, index) => {
            const cell = document.createElement('td');
            cell.textContent = value;
            if (index === 2) { cell.style.maxWidth = '520px'; cell.title = ref; }
            row.appendChild(cell);
          });
          table.appendChild(row);
        });
        $('search-note').textContent = body.count >= 50
          ? '상위 50건만 보여줍니다. 검색어를 좁히세요.' : '';
      } catch (error) { line.textContent = error.message; }
    }

    async function loadAudit() {
      try {
        const body = await api('/api/v1/admin/audit?limit=100');
        const table = $('audit-body'); table.textContent = '';
        if (!body.items.length) {
          const row = document.createElement('tr'); const cell = document.createElement('td'); cell.colSpan = 3; cell.textContent = '기록이 없습니다.'; row.appendChild(cell); table.appendChild(row); return;
        }
        body.items.forEach((item) => {
          const row = document.createElement('tr');
          [new Date(item.at).toLocaleString(), item.action, JSON.stringify(item.details || {})].forEach((value) => { const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell); });
          table.appendChild(row);
        });
      } catch (_) {}
    }

    // Labels and columns are the server's schema, fetched once from
    // /api/v1/admin/work/meta.  These literals are only the fallback for a
    // meta request that failed, and they must stay a *total* partition of the
    // status set for the same reason the server's do.
    let WORK_STATUS_LABELS = {
      in_progress: '진행 중', ready: '다음 할 일', todo: '해야 할 일', backlog: '백로그',
      waiting: '대기', blocked: '막힘', done: '완료', cancelled: '취소',
    };
    const WORK_PRIORITY_LABELS = { urgent: '긴급', high: '높음', normal: '보통', low: '낮음' };
    let WORK_COLUMNS = [
      { key: 'in_progress', title: '진행 중', statuses: ['in_progress'] },
      { key: 'ready', title: '다음 할 일', statuses: ['ready'] },
      { key: 'todo', title: '해야 할 일', statuses: ['todo'] },
      { key: 'backlog', title: '백로그', statuses: ['backlog'] },
      { key: 'held', title: '대기 · 막힘', statuses: ['waiting', 'blocked'] },
      { key: 'closed', title: '최근 완료', statuses: ['done', 'cancelled'], recentDays: 14 },
    ];
    const WORK_DOCUMENT_VERSION = 2;
    const WORK_RESIDUE = { key: 'unclassified', title: '미분류' };
    const WORK_POLL_MS = 15000;
    const workState = {
      items: [], loaded: false, timer: null, editing: null, busy: false, assignee: '',
      total: 0, statusCounts: {}, metaLoaded: false, migratedFrom: null,
    };

    async function loadWorkMeta() {
      if (workState.metaLoaded) return;
      try {
        const meta = await api('/api/v1/admin/work/meta');
        if (meta.status_labels) WORK_STATUS_LABELS = meta.status_labels;
        if (Array.isArray(meta.columns) && meta.columns.length) {
          WORK_COLUMNS = meta.columns.map((column) => ({
            key: column.key, title: column.title, statuses: column.statuses,
            recentDays: column.recent_days,
          }));
        }
        if (meta.residue_column) { WORK_RESIDUE.key = meta.residue_column.key; WORK_RESIDUE.title = meta.residue_column.title; }
        workState.metaLoaded = true;
        fillWorkSelect('work-status', WORK_STATUS_LABELS);
      } catch (_) {
        // Keep the built-in fallback rather than rendering an empty board.
      }
    }

    function workRelativeTime(value) {
      if (!value) return '-';
      const moment = new Date(value);
      if (Number.isNaN(moment.getTime())) return '-';
      const minutes = Math.round((Date.now() - moment.getTime()) / 60000);
      if (minutes < 1) return '방금';
      if (minutes < 60) return `${minutes}분 전`;
      if (minutes < 1440) return `${Math.round(minutes / 60)}시간 전`;
      if (minutes < 20160) return `${Math.round(minutes / 1440)}일 전`;
      return moment.toLocaleDateString();
    }

    function workChip(label, className = '') {
      const node = document.createElement('span');
      node.className = `work-chip${className ? ' ' + className : ''}`;
      node.textContent = label;
      return node;
    }

    function workLine(label, value) {
      const node = document.createElement('div');
      node.className = 'work-line';
      const name = document.createElement('b');
      name.textContent = `${label} `;
      node.appendChild(name);
      node.appendChild(document.createTextNode(value));
      return node;
    }

    function workCard(item, byId) {
      const card = document.createElement('article');
      card.className = 'work-card';
      const title = document.createElement('div');
      title.className = 'work-card-title';
      title.textContent = item.title;
      card.appendChild(title);

      const chips = document.createElement('div');
      chips.className = 'work-chips';
      // The programme phase, first and marked, so a column reads P0 / P1 / P2
      // at a glance. An item with no phase gets no chip rather than a blank
      // one: not placed yet is a visible state, not an empty label.
      if (item.phase) chips.appendChild(workChip(item.phase, 'phase'));
      chips.appendChild(workChip(item.assigned_to, 'assignee'));
      chips.appendChild(workChip(WORK_PRIORITY_LABELS[item.priority] || item.priority, `priority-${item.priority}`));
      chips.appendChild(workChip(WORK_STATUS_LABELS[item.status] || item.status));
      // 언제 마무리 되는지. A live item with no date says so rather than
      // showing nothing, because an absent deadline is the thing worth
      // seeing — it is what the audit now counts.
      if (!['done', 'cancelled'].includes(item.status)) {
        if (item.due_at) {
          const late = new Date(item.due_at) < new Date();
          chips.appendChild(workChip(
            `마감 ${item.due_at.slice(0, 10)}${late ? ' 지남' : ''}`,
            late ? 'due late' : 'due'));
        } else if (['in_progress', 'ready'].includes(item.status)) {
          chips.appendChild(workChip('마감 없음', 'due late'));
        }
      }
      chips.appendChild(workChip(workRelativeTime(item.updated_at)));
      if (item.parent_id && byId.has(item.parent_id)) chips.appendChild(workChip(`상위: ${byId.get(item.parent_id).title}`));
      card.appendChild(chips);

      if (item.progress_summary) card.appendChild(workLine('진행', item.progress_summary));
      if (item.next_action) card.appendChild(workLine('다음', item.next_action));
      if (item.blocker) {
        const blocker = workLine('막힘', item.blocker);
        blocker.style.color = 'var(--danger)';
        card.appendChild(blocker);
      }
      if (item.due_at) card.appendChild(workLine('마감', new Date(item.due_at).toLocaleDateString()));

      const actions = document.createElement('div');
      actions.className = 'work-card-actions';
      const select = document.createElement('select');
      select.setAttribute('aria-label', '상태 변경');
      Object.keys(WORK_STATUS_LABELS).forEach((status) => {
        const option = document.createElement('option');
        option.value = status; option.textContent = WORK_STATUS_LABELS[status];
        if (status === item.status) option.selected = true;
        select.appendChild(option);
      });
      select.addEventListener('change', () => changeWorkStatus(item, select.value));
      actions.appendChild(select);

      const edit = document.createElement('button');
      edit.type = 'button'; edit.className = 'button'; edit.textContent = '편집';
      edit.addEventListener('click', () => openWorkEditor(item));
      actions.appendChild(edit);

      const timeline = document.createElement('button');
      timeline.type = 'button'; timeline.className = 'button'; timeline.textContent = '이력';
      timeline.addEventListener('click', () => openTimeline(item));
      actions.appendChild(timeline);

      const archive = document.createElement('button');
      archive.type = 'button'; archive.className = 'button danger'; archive.textContent = '보관';
      archive.addEventListener('click', () => archiveWork(item));
      actions.appendChild(archive);
      card.appendChild(actions);
      return card;
    }

    function workVisibleItems() {
      return workState.items.filter((item) => !workState.assignee || item.assigned_to === workState.assignee);
    }

    function workColumnItems(visible) {
      // The board is a total partition of what it was given: every item lands
      // in exactly one column, or in the residue column, or is counted as aged
      // out of a dated column.  A status no column claims must be loud, not
      // invisible -- that is how an in_progress item went missing before.
      const placed = new Set();
      const columns = WORK_COLUMNS.map((column) => {
        const items = visible.filter((item) => {
          if (!column.statuses.includes(item.status)) return false;
          if (column.recentDays) {
            const stamp = new Date(item.completed_at || item.updated_at).getTime();
            if (Number.isFinite(stamp) && stamp < Date.now() - column.recentDays * 86400000) return false;
          }
          placed.add(item.id);
          return true;
        });
        return { ...column, items };
      });
      const claimed = new Set(WORK_COLUMNS.flatMap((column) => column.statuses));
      const leftover = visible.filter((item) => !placed.has(item.id));
      const unclaimed = leftover.filter((item) => !claimed.has(item.status));
      columns.push({ ...WORK_RESIDUE, statuses: [], items: unclaimed, residue: true });
      return { columns, agedOut: leftover.length - unclaimed.length };
    }

    function renderWork() {
      const board = $('work-board');
      board.textContent = '';
      const byId = new Map(workState.items.map((item) => [item.id, item]));
      const visible = workVisibleItems();
      const { columns, agedOut } = workColumnItems(visible);
      columns.forEach((column) => {
        if (column.residue && !column.items.length) return;
        const section = document.createElement('section');
        section.className = `work-column${column.residue ? ' residue' : ''}`;
        const header = document.createElement('header');
        const heading = document.createElement('h2');
        heading.textContent = column.title;
        const count = document.createElement('span');
        count.className = 'badge';
        count.textContent = String(column.items.length);
        header.appendChild(heading); header.appendChild(count);
        section.appendChild(header);
        if (column.residue) {
          const warning = document.createElement('p');
          warning.className = 'work-empty';
          warning.style.color = 'var(--danger)';
          warning.textContent = '어느 단계에도 속하지 않는 상태입니다. 화면이 스키마를 따라가지 못하고 있습니다.';
          section.appendChild(warning);
        }
        const list = document.createElement('div');
        list.className = 'work-list';
        if (!column.items.length) {
          const empty = document.createElement('p');
          empty.className = 'work-empty';
          empty.textContent = '해당하는 업무가 없습니다.';
          list.appendChild(empty);
        } else {
          column.items.forEach((item) => list.appendChild(workCard(item, byId)));
        }
        section.appendChild(list);
        board.appendChild(section);
      });
      renderWorkHint(visible, agedOut);
      renderWorkAssigneeFilter();
    }

    function renderWorkHint(visible, agedOut) {
      // Anything the board is not showing is stated here.  A filtered count
      // must never be able to pass for the complete picture.
      const notes = [];
      const hidden = workState.total - visible.length;
      if (workState.assignee) notes.push(`담당 '${workState.assignee}' 필터가 켜져 있어 ${hidden}건을 숨기고 있습니다`);
      else if (hidden > 0) notes.push(`${hidden}건이 목록에 없습니다`);
      if (agedOut > 0) notes.push(`완료된 지 오래된 ${agedOut}건은 '최근 완료'에서 제외했습니다`);
      if (workState.migratedFrom) notes.push(`저장 문서가 v${workState.migratedFrom}이며 다음 저장에서 v${WORK_DOCUMENT_VERSION}로 올라갑니다`);
      const node = $('work-hint');
      node.textContent = '';
      if (!notes.length) return;
      const strong = document.createElement('b');
      strong.textContent = '표시 범위: ';
      node.appendChild(strong);
      node.appendChild(document.createTextNode(notes.join(' · ')));
    }

    function renderWorkAssigneeFilter() {
      const select = $('work-filter-assignee');
      const assignees = [...new Set(workState.items.map((item) => item.assigned_to))].sort();
      if (select.dataset.signature === assignees.join(',')) return;
      select.dataset.signature = assignees.join(',');
      select.textContent = '';
      const all = document.createElement('option');
      all.value = ''; all.textContent = '담당 전체';
      select.appendChild(all);
      assignees.forEach((name) => {
        const option = document.createElement('option');
        option.value = name; option.textContent = name;
        select.appendChild(option);
      });
      select.value = assignees.includes(workState.assignee) ? workState.assignee : '';
      workState.assignee = select.value;
    }

    function setWorkState(message, isError = false) {
      const node = $('work-state');
      node.textContent = message;
      node.className = isError ? 'work-state error' : 'work-state';
    }

    async function loadWork({ quiet = false } = {}) {
      // A poll must never discard what the operator is typing.
      if (quiet && (workState.editing || workState.busy)) return;
      if (!quiet && !workState.loaded) setWorkState('불러오는 중…');
      try {
        await loadWorkMeta();
        const body = await api('/api/v1/admin/work/items');
        workState.items = body.items || [];
        workState.total = typeof body.total === 'number' ? body.total : workState.items.length;
        workState.statusCounts = body.status_counts || {};
        workState.migratedFrom = body.migrated_from || null;
        workState.withheld = body.withheld || null;
        workState.loaded = true;
        if (workState.editing || workState.busy) return;
        renderWork();
        // Say what is not on the board as well as what is. An archived item
        // that never finished is work that left the board while still being
        // work, and a count of zero is itself worth showing.
        // Who was last seen, beside what they still owe. An item assigned to
        // someone who has left no recent trace is not in progress, and until
        // this row existed the only way to know was to read a hidden file.
        api('/api/v1/admin/work/agents').then((payload) => {
          const line = (payload.agents || []).map((row) => {
            const mark = row.verdict === '활동 있음' ? '' : ` ${row.verdict}`;
            const ago = row.seconds_since === null
              ? '기록 없음'
              : `${Math.round(row.seconds_since / 60)}분 전`;
            return `${row.agent} ${ago}${mark} (열림 ${row.open_items})`;
          }).join(' · ');
          $('work-agents').textContent = line
            ? `${line} · 기준 ${Math.round((payload.quiet_after_seconds || 0) / 60)}분`
            : '';
        }).catch(() => { $('work-agents').textContent = ''; });

        const withheld = workState.withheld;
        const hidden = withheld && !withheld.included && withheld.archived
          ? ` · 보관 ${withheld.archived}건(미완 ${withheld.archived_unfinished})`
          : '';
        setWorkState(`업무 ${workVisibleItems().length}건 표시 · 전체 ${workState.total}건${hidden} · 마지막 확인 ${new Date().toLocaleTimeString()}`);
      } catch (error) {
        setWorkState(`업무 정보를 불러오지 못했습니다: ${error.message}`, true);
      }
    }

    function startWorkPolling() {
      stopWorkPolling();
      workState.timer = setInterval(() => loadWork({ quiet: true }), WORK_POLL_MS);
    }

    function stopWorkPolling() {
      if (workState.timer) { clearInterval(workState.timer); workState.timer = null; }
    }

    function fillWorkSelect(id, labels) {
      const select = $(id);
      select.textContent = '';
      Object.keys(labels).forEach((value) => {
        const option = document.createElement('option');
        option.value = value; option.textContent = labels[value];
        select.appendChild(option);
      });
    }

    function openWorkEditor(item) {
      // A non-null `editing` marks the form as active, so polling never
      // re-renders the board underneath what the operator is typing.
      workState.editing = item ? { ...item } : { id: null };
      $('work-dialog-title').textContent = item ? '업무 편집' : '업무 추가';
      $('work-dialog-notice').textContent = '';
      const parent = $('work-parent');
      parent.textContent = '';
      const none = document.createElement('option');
      none.value = ''; none.textContent = '없음';
      parent.appendChild(none);
      workState.items
        .filter((candidate) => !item || candidate.id !== item.id)
        .forEach((candidate) => {
          const option = document.createElement('option');
          option.value = candidate.id; option.textContent = candidate.title;
          parent.appendChild(option);
        });
      $('work-title').value = item ? item.title : '';
      $('work-assigned').value = item ? item.assigned_to : '';
      $('work-requested').value = item ? item.requested_by : '';
      $('work-status').value = item ? item.status : 'backlog';
      $('work-priority').value = item ? item.priority : 'normal';
      $('work-phase').value = (item && item.phase) || '';
      parent.value = item && item.parent_id ? item.parent_id : '';
      $('work-due').value = item && item.due_at ? item.due_at.slice(0, 10) : '';
      $('work-progress').value = item ? item.progress_summary || '' : '';
      $('work-next').value = item ? item.next_action || '' : '';
      $('work-blocker').value = item ? item.blocker || '' : '';
      $('work-detail').value = item ? item.detail || '' : '';
      $('work-overlay').classList.remove('hidden');
      $('work-title').focus();
    }

    function closeWorkEditor() {
      workState.editing = null;
      $('work-overlay').classList.add('hidden');
    }

    function workFormFields() {
      return {
        title: $('work-title').value.trim(),
        assigned_to: $('work-assigned').value.trim(),
        requested_by: $('work-requested').value.trim(),
        status: $('work-status').value,
        priority: $('work-priority').value,
        phase: $('work-phase').value.trim().toUpperCase(),
        parent_id: $('work-parent').value || null,
        due_at: $('work-due').value || null,
        progress_summary: $('work-progress').value.trim(),
        next_action: $('work-next').value.trim(),
        blocker: $('work-blocker').value.trim() || null,
        detail: $('work-detail').value.trim() || null,
      };
    }

    $('work-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const fields = workFormFields();
      const editing = workState.editing && workState.editing.id ? workState.editing : null;
      workState.busy = true;
      try {
        if (editing) {
          // Send only what changed, so a concurrent edit to another field survives.
          const changed = {};
          Object.keys(fields).forEach((key) => {
            const before = key === 'due_at' ? (editing.due_at || '').slice(0, 10) || null : (editing[key] ?? null);
            const after = fields[key] === '' && before === null ? null : fields[key];
            if (JSON.stringify(before) !== JSON.stringify(after)) changed[key] = after;
          });
          if (!Object.keys(changed).length) { closeWorkEditor(); workState.busy = false; return; }
          await api(`/api/v1/admin/work/items/${editing.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ fields: changed, expected_revision: editing.revision }),
          });
        } else {
          await api('/api/v1/admin/work/items', { method: 'POST', body: JSON.stringify({ fields }) });
        }
        closeWorkEditor();
        workState.busy = false;
        await loadWork();
        toast(editing ? '업무를 수정했습니다.' : '업무를 추가했습니다.');
      } catch (error) {
        workState.busy = false;
        $('work-dialog-notice').textContent = error.message;
      }
    });

    async function changeWorkStatus(item, status) {
      if (status === item.status) return;
      workState.busy = true;
      try {
        await api(`/api/v1/admin/work/items/${item.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ fields: { status }, expected_revision: item.revision }),
        });
        workState.busy = false;
        await loadWork();
        toast(`상태를 ${WORK_STATUS_LABELS[status]}(으)로 바꿨습니다.`);
      } catch (error) {
        workState.busy = false;
        toast(error.message, true);
        await loadWork();
      }
    }

    async function archiveWork(item) {
      if (!confirm(`"${item.title}" 업무를 보관할까요? 목록에서만 사라지고 기록은 남습니다.`)) return;
      workState.busy = true;
      try {
        await api(`/api/v1/admin/work/items/${item.id}/archive`, {
          method: 'POST',
          body: JSON.stringify({ expected_revision: item.revision }),
        });
        workState.busy = false;
        await loadWork();
        toast('업무를 보관했습니다.');
      } catch (error) {
        workState.busy = false;
        toast(error.message, true);
        await loadWork();
      }
    }

    $('work-add').addEventListener('click', () => openWorkEditor(null));
    $('work-cancel').addEventListener('click', closeWorkEditor);
    $('work-refresh').addEventListener('click', () => loadWork());
    $('work-filter-assignee').addEventListener('change', (event) => {
      workState.assignee = event.target.value;
      renderWork();
      setWorkState(`업무 ${workVisibleItems().length}건 표시 · 전체 ${workState.total}건 · 마지막 확인 ${new Date().toLocaleTimeString()}`);
    });
    fillWorkSelect('work-status', WORK_STATUS_LABELS);
    fillWorkSelect('work-priority', WORK_PRIORITY_LABELS);


    // -------------------------------------------------------- 활동 타임라인
    const TIMELINE_PHASE_LABELS = {
      assigned: '배정', started: '착수', progress: '진행', review: '검수',
      build: '빌드', deploy: '배포', verified: '검증', failed: '실패',
      change: '변경', test: '테스트', waiting: '대기', handoff: '인계',
      start: '시작', failure: '실패', completed: '완료',
    };
    const TIMELINE_SOURCE_LABELS = {
      work_history: '업무 기록', cowork_event: 'Cowork 이벤트', cowork_handoff: 'Cowork 인계',
    };
    const TIMELINE_RESOLUTION_LABELS = {
      declared: '명시', inferred: '추론', unresolved: '불명',
    };

    function timelineActor(resolved) {
      if (!resolved || !resolved.name) return '행위자 불명';
      const label = TIMELINE_RESOLUTION_LABELS[resolved.resolution] || resolved.resolution;
      return `${resolved.name} (${label})`;
    }

    function timelineEntryNode(entry) {
      const node = document.createElement('article');
      node.className = `timeline-entry${entry.record_schema === 'legacy' ? ' legacy' : ''}`;

      const head = document.createElement('div');
      head.className = 'timeline-head';
      head.appendChild(collectionPill(
        TIMELINE_SOURCE_LABELS[entry.source] || entry.source, 'unknown'));
      if (entry.phase) {
        head.appendChild(collectionPill(
          TIMELINE_PHASE_LABELS[entry.phase] || entry.phase, 'running'));
      }
      if (entry.status) {
        head.appendChild(collectionPill(
          WORK_STATUS_LABELS[entry.status] || entry.status, 'collected'));
      }
      const when = document.createElement('span');
      when.className = 'timeline-when';
      when.textContent = collectionWhen(entry.at);
      head.appendChild(when);
      node.appendChild(head);

      const lines = [];
      lines.push(`행위자 ${timelineActor(entry.actor)}` +
        (entry.directed_by ? ` · 지시 ${timelineActor(entry.directed_by)}` : ''));
      const who = [];
      if (entry.requested_by) who.push(`요청 ${entry.requested_by}`);
      if (entry.assigned_to) who.push(`담당 ${entry.assigned_to}`);
      if (entry.revision !== null && entry.revision !== undefined) who.push(`rev ${entry.revision}`);
      if (entry.action) who.push(entry.action);
      if (who.length) lines.push(who.join(' · '));
      if (entry.status_from) lines.push(`상태 ${WORK_STATUS_LABELS[entry.status_from] || entry.status_from} → ${WORK_STATUS_LABELS[entry.status] || entry.status}`);
      if (entry.fields && entry.fields.length) lines.push(`변경 필드: ${entry.fields.join(', ')}`);
      if (entry.summary) lines.push(entry.summary);
      if (entry.session_id) lines.push(`세션 ${entry.session_id}`);
      if (entry.receipt) lines.push(`영수증 ${entry.receipt}`);
      if (entry.handoff_id) lines.push(`인계 ${entry.handoff_id}`);
      if (entry.record_schema === 'legacy') {
        lines.push(`이 기록은 타임라인 필드가 생기기 전에 쓰였습니다. 미기록: ${(entry.unknown_fields || []).join(', ')}`);
      }
      lines.forEach((text) => { const n = collectionNote(text); if (n) node.appendChild(n); });
      return node;
    }

    async function openTimeline(item) {
      $('timeline-overlay').classList.remove('hidden');
      $('timeline-title').textContent = item.title;
      // Who directed it, who is carrying it, who checks it, and where it
      // stands. The reviewer is read off the review item rather than stored
      // here, so it says plainly when no review item points at this one.
      $('timeline-subtitle').textContent = `${item.id} · 불러오는 중…`;
      api(`/api/v1/admin/work/items/${encodeURIComponent(item.id)}`).then((detail) => {
        const roles = detail.roles || {};
        const reviewers = (roles.reviewed_by || []).length
          ? (roles.reviewed_by || []).join(', ')
          : '없음';
        const stage = roles.stage
          ? `${roles.stage.label}${roles.stage.kind === 'condition' ? ' (조건)' : ''}`
          : item.status;
        $('timeline-subtitle').textContent =
          `${item.id} · 지시 ${roles.directed_by || '-'} · 수행 ${roles.performed_by || '-'}`
          + ` · 검수 ${reviewers} · 단계 ${stage}`;
        $('timeline-subtitle').title = roles.reviewed_by_basis || '';
      }).catch(() => {
        $('timeline-subtitle').textContent =
          `${item.id} · 요청 ${item.requested_by} · 담당 ${item.assigned_to}`;
      });
      $('timeline-list').textContent = '';
      $('timeline-state').textContent = '불러오는 중…';
      $('timeline-state').className = 'work-state';
      try {
        const payload = await api(`/api/v1/admin/work/items/${encodeURIComponent(item.id)}/timeline?limit=200`);
        const list = $('timeline-list');
        list.textContent = '';
        if (!payload.entries.length) {
          $('timeline-state').textContent = '이 업무에는 아직 기록된 활동이 없습니다.';
          return;
        }
        payload.entries.slice().reverse().forEach((entry) => list.appendChild(timelineEntryNode(entry)));
        const legacy = payload.entries.filter((e) => e.record_schema === 'legacy').length;
        $('timeline-state').textContent =
          `${payload.count}건${payload.truncated ? ' (상한까지만 표시)' : ''}` +
          (legacy ? ` · 이전 형식 ${legacy}건은 일부 필드가 없습니다` : '');
      } catch (error) {
        $('timeline-state').textContent = `타임라인을 불러오지 못했습니다: ${error.message}`;
        $('timeline-state').className = 'work-state error';
      }
    }

    function closeTimeline() { $('timeline-overlay').classList.add('hidden'); }
    $('timeline-close').addEventListener('click', closeTimeline);
    $('timeline-overlay').addEventListener('click', (event) => {
      if (event.target === $('timeline-overlay')) closeTimeline();
    });

    // ------------------------------------------------------------ 수집 현황
    const COLLECTION_SOURCE_LABELS = {
      slack: 'Slack', notion: 'Notion', 'google-calendar': 'Google Calendar',
      google_calendar: 'Google Calendar', github: 'GitHub',
    };
    const COVERAGE_LABELS = {
      collected: '수집', collected_with_skips: '수집(누락 명시)', partial: '부분수집',
      unverified: '레거시만(검증 불가)', running: '수집 중', failed: '실패',
      not_collected: '미수집', unknown: '불명',
      // Not a weaker `unverified`: nobody opened the record, so this date has
      // no verdict at all in this range. Narrowing the range produces one.
      unexamined: '레거시 기록 미확인(범위 넓음)',
    };
    const TIME_COVERAGE_LABELS = {
      complete: '하루 전체 관측', partial: '뒷부분 미관측', in_progress: '진행 중인 날짜',
    };

    function coverageKst(iso) {
      if (!iso) return null;
      const moment = new Date(iso);
      if (Number.isNaN(moment.getTime())) return null;
      return new Intl.DateTimeFormat('ko-KR', {
        hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul',
      }).format(moment);
    }

    // Observation quality and time coverage are separate axes. A date whose
    // remaining hours have not happened yet cannot be called collected, however
    // clean the runs that touched it are.
    function coverageBadge(cell) {
      const quality = cell.coverage;
      const time = cell.time_coverage;
      const settled = quality === 'collected' || quality === 'collected_with_skips';
      if (settled && time === 'in_progress') {
        const at = coverageKst(cell.observed_through);
        return { key: 'running', text: at ? `진행 중 (${at}까지)` : '진행 중' };
      }
      if (settled && time === 'partial') {
        const at = coverageKst(cell.observed_through);
        return { key: 'partial', text: at ? `부분수집 (${at}까지)` : '부분수집' };
      }
      return { key: quality, text: COVERAGE_LABELS[quality] || quality };
    }
    const EVIDENCE_LABELS = {
      manifest: '근거: 실행 매니페스트',
      directory_only: '근거: 레거시 디렉터리뿐 (완전성을 주장할 수 없음)',
      mixed: '근거: 실행 매니페스트 + 레거시 디렉터리',
    };
    const RUN_STATE_LABELS = {
      success: '성공', success_with_skips: '성공 (스킵 있음)', degraded: '저하', failed: '실패',
      running: '진행 중', stale: '중단 추정', malformed: 'manifest 손상', unknown: '불명',
    };
    const RULE_ATTRIBUTION_LABELS = {
      declared: '명시', inferred: '추론', legacy: 'legacy', unknown: '불명',
    };
    const DENSITY_LABELS = {
      day_slice: '일자 슬라이스', incremental_continuous: '연속 증분', unknown: '불명',
    };
    const COMPLETENESS_LABELS = { complete: '완전', incomplete: '불완전', unknown: '불명' };
    const COLLECTION_POLL_MS = 20000;
    const collectionState = { timer: null, rulesLoaded: false, busy: false, environments: [], overviewAt: null, coverageAt: null, scope: 'production' };

    function collectionBytes(value) {
      if (value === null || value === undefined) return '불명';
      const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
      let size = Number(value); let index = 0;
      while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
      return `${index === 0 ? size : size.toFixed(1)} ${units[index]}`;
    }

    function collectionWhen(value, { dateOnly = false } = {}) {
      if (!value) return '-';
      const moment = new Date(value);
      if (Number.isNaN(moment.getTime())) return '-';
      return dateOnly ? moment.toLocaleDateString() : moment.toLocaleString();
    }

    function collectionPill(text, className) {
      const node = document.createElement('span');
      node.className = `cov ${className || ''}`.trim();
      node.textContent = text;
      return node;
    }

    function collectionCell(row, ...nodes) {
      const cell = document.createElement('td');
      const wrap = document.createElement('div');
      wrap.className = 'collection-cell';
      nodes.filter(Boolean).forEach((node) => wrap.appendChild(node));
      cell.appendChild(wrap);
      row.appendChild(cell);
      return cell;
    }

    function collectionNote(text, title) {
      if (text === null || text === undefined || text === '') return null;
      const node = document.createElement('small');
      node.textContent = text;
      if (title) node.title = title;
      return node;
    }

    function collectionRuleLabel(rule) {
      if (!rule || !rule.version) return '불명';
      const attribution = RULE_ATTRIBUTION_LABELS[rule.attribution] || rule.attribution || '불명';
      // A run stamps whatever rule was active when it started, and that rule
      // may say nothing about the source it captured. Saying so is the point:
      // the manifest is honest, and neither failing on it nor filling the gap
      // in with the current rule would be.
      const undeclared = rule.declares_source === false ? ' · 규칙이 이 소스를 선언 안 함' : '';
      return `${rule.version} · ${attribution}${undeclared}`;
    }

    function setCollectionState(message, isError = false) {
      const node = $('collection-state');
      node.textContent = message;
      node.className = `work-state${isError ? ' error' : ''}`;
    }

    function renderCollectionCards(overview) {
      const host = $('collection-cards');
      host.textContent = '';
      overview.cards.forEach((card) => {
        const article = document.createElement('article');
        article.className = 'card';
        const label = document.createElement('div');
        label.className = 'label';
        label.textContent = COLLECTION_SOURCE_LABELS[card.source] || card.source;
        article.appendChild(label);

        const active = (card.active || [])[0];
        const metric = document.createElement('div');
        metric.className = 'metric';
        metric.style.fontSize = '17px';
        const last = card.last_run;
        metric.textContent = active
          ? (RUN_STATE_LABELS[active.state] || active.state)
          : (last ? (RUN_STATE_LABELS[last.state] || last.state) : '실행 기록 없음');
        article.appendChild(metric);

        const lines = [];
        if (active) {
          lines.push(`진행 run ${active.run_id} · ${active.environment}`);
          lines.push(`원본 ${active.raw_file_count ?? '불명'}개 · ${collectionBytes(active.raw_bytes)}`);
          lines.push(`최종 활동 ${collectionWhen(active.last_activity_at)}`);
          if (active.state_reason) lines.push(active.state_reason);
        } else if (last) {
          lines.push(`마지막 run ${last.run_id} · ${last.environment}`);
          lines.push(`종료 ${collectionWhen(last.finished_at || last.last_activity_at)}`);
          lines.push(`규칙 ${collectionRuleLabel(last.rule)}`);
        }
        if (card.last_success) {
          lines.push(`마지막 성공 ${collectionWhen(card.last_success.finished_at || card.last_success.last_activity_at)}`);
        }
        lines.push(`실행 ${card.runs}건 · ${Object.entries(card.states).map(([state, count]) => `${RUN_STATE_LABELS[state] || state} ${count}`).join(', ') || '없음'}`);
        lines.forEach((text) => {
          const line = document.createElement('div');
          line.className = 'work-line';
          line.textContent = text;
          article.appendChild(line);
        });
        host.appendChild(article);
      });
    }

    function renderCollectionRuns(payload) {
      const body = $('collection-runs-body');
      body.textContent = '';
      const runs = payload.recent_runs || payload.items || [];
      if (!runs.length) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 12; cell.textContent = '실행 기록이 없습니다.';
        row.appendChild(cell); body.appendChild(row); return;
      }
      runs.forEach((run) => {
        const row = document.createElement('tr');
        collectionCell(row,
          collectionNote(COLLECTION_SOURCE_LABELS[run.source] || run.source),
          collectionNote(run.environment));
        collectionCell(row, collectionNote(run.run_id));
        const status = collectionPill(RUN_STATE_LABELS[run.state] || run.state, run.state);
        collectionCell(row, status,
          collectionNote(run.state_reason || run.malformed_reason || null),
          collectionNote(run.capture_density ? `밀도 ${run.capture_density}${run.dry_run ? ' · dry-run' : ''}` : null));
        collectionCell(row, collectionNote(collectionWhen(run.started_at)));
        collectionCell(row, collectionNote(collectionWhen(run.finished_at)));
        collectionCell(row, collectionNote(collectionWhen(run.last_activity_at)));
        collectionCell(row, collectionNote(
          run.checkpoint_advanced === true ? '전진' : run.checkpoint_advanced === false ? '유지' : '불명'));
        collectionCell(row,
          collectionNote(`${run.raw_file_count ?? '불명'}개 · ${collectionBytes(run.raw_bytes)}`),
          collectionNote(run.raw_from_manifest ? 'manifest 기준' : '디렉터리 스캔'),
          collectionNote(run.raw_scan_truncated ? '스캔 상한 도달' : null));
        collectionCell(row,
          collectionNote(`레코드 ${run.ledger_records ?? '불명'}`, run.ledger_reason || ''),
          collectionNote(`스키마 오류 ${run.ledger_schema_errors_known ? run.ledger_schema_errors : '불명'}`));
        const skipKinds = (run.skip_kinds || []).map((entry) => `${entry.kind} ${entry.count}`).join(', ');
        const errorKinds = (run.error_kinds || []).map((entry) => `${entry.kind} ${entry.count}`).join(', ');
        collectionCell(row,
          collectionNote(`skip ${run.skips_total ?? '불명'} · failure ${run.errors_total ?? '불명'}`),
          collectionNote(skipKinds || null, skipKinds),
          collectionNote(errorKinds || null, errorKinds),
          collectionNote(run.truncated ? `truncated ${run.truncation_total ?? ''}`.trim() : null));
        collectionCell(row,
          collectionNote(collectionRuleLabel(run.rule)),
          collectionNote(run.rule && run.rule.digest ? run.rule.digest.slice(0, 19) : null,
            run.rule ? (run.rule.digest || '') : ''),
          collectionNote(run.rule && run.rule.evidence && run.rule.evidence.length
            ? `근거 ${run.rule.evidence.join(', ')}` : null));
        const evidence = run.manifest_path || run.raw_run_dir || '-';
        collectionCell(row,
          collectionNote(evidence, evidence),
          collectionNote(run.manifest_revisions > 1 ? `manifest ${run.manifest_revisions}회 기록` : null));
        body.appendChild(row);
      });
      const errors = payload.manifest_errors || [];
      $('collection-runs-note').textContent = errors.length
        ? `읽을 수 없는 manifest ${errors.length}건은 성공으로 집계하지 않고 격리했습니다: ${errors.map((item) => item.manifest_path || item.run_id).join(', ')}`
        : '읽을 수 없는 manifest는 없습니다.';
      $('collection-runs-badge').textContent = `${runs.length}건 표시`;
    }

    function coverageCellNode(cell) {
      const wrap = document.createElement('div');
      wrap.className = 'collection-cell';
      const badge = coverageBadge(cell);
      wrap.appendChild(collectionPill(badge.text, badge.key));
      if (cell.time_coverage) {
        const node = collectionNote(`시간 커버리지: ${TIME_COVERAGE_LABELS[cell.time_coverage] || cell.time_coverage}`);
        if (node) {
          node.title = `observed_through: ${cell.observed_through || '없음'}`;
          wrap.appendChild(node);
        }
      }
      const versions = (cell.rule_versions || [])
        .map((entry) => `${entry.version || '불명'}·${RULE_ATTRIBUTION_LABELS[entry.attribution] || entry.attribution}${entry.count ? ` ×${entry.count}` : ''}`)
        .join(', ');
      const runs = cell.runs_known ? `실행 ${cell.runs}회` : '실행 횟수 불명';
      [
        versions || '규칙 근거 없음',
        `${runs} · 최종 ${cell.last_status ? (RUN_STATE_LABELS[cell.last_status] || cell.last_status) : '-'}`,
        `밀도 ${DENSITY_LABELS[cell.density] || '불명'} · 완전성 ${COMPLETENESS_LABELS[cell.completeness] || '불명'}`,
      ].forEach((text) => { const node = collectionNote(text); if (node) wrap.appendChild(node); });
      if (cell.evidence && cell.evidence.length) {
        const node = collectionNote(cell.evidence[0] + (cell.evidence.length > 1 ? ` 외 ${cell.evidence.length - 1}건` : ''), cell.evidence.join('\n'));
        if (node) wrap.appendChild(node);
      }
      if (cell.evidence_class) {
        const node = collectionNote(EVIDENCE_LABELS[cell.evidence_class] || cell.evidence_class);
        if (node) { node.title = `evidence_class: ${cell.evidence_class}`; wrap.appendChild(node); }
      }
      (cell.notes || []).forEach((text) => { const node = collectionNote(text); if (node) wrap.appendChild(node); });
      return wrap;
    }

    function renderCoverage(payload) {
      const head = $('coverage-head');
      const body = $('coverage-body');
      head.textContent = ''; body.textContent = '';
      const headRow = document.createElement('tr');
      [payload.group === 'weekday' ? '요일' : '날짜 (KST)', ...payload.sources.map((source) => COLLECTION_SOURCE_LABELS[source] || source)]
        .forEach((label) => { const cell = document.createElement('th'); cell.textContent = label; headRow.appendChild(cell); });
      head.appendChild(headRow);

      if (payload.group === 'weekday') {
        (payload.weekday_rows || []).forEach((row) => {
          const tr = document.createElement('tr');
          const first = document.createElement('td');
          first.textContent = `${row.weekday} (${row.dates}일)`;
          tr.appendChild(first);
          payload.sources.forEach((source) => {
            const cell = row.cells[source] || {};
            const td = document.createElement('td');
            const wrap = document.createElement('div');
            wrap.className = 'collection-cell';
            Object.entries(cell.coverage_counts || {}).forEach(([key, count]) => {
              wrap.appendChild(collectionPill(`${COVERAGE_LABELS[key] || key} ${count}`, key));
            });
            const versions = Object.entries(cell.rule_versions || {}).map(([label, count]) => `${label} ×${count}`).join(', ');
            [
              cell.runs_known ? `실행 합계 ${cell.runs}회` : '실행 횟수 불명',
              versions || '규칙 근거 없음',
              `미수집 ${cell.dates_not_collected}일`,
              // Only when there are some: a steady "0일" would train the eye
              // to skip the line on the days it matters.
              cell.dates_unexamined ? `레거시 기록 미확인 ${cell.dates_unexamined}일` : null,
            ].forEach((text) => { const node = collectionNote(text); if (node) wrap.appendChild(node); });
            td.appendChild(wrap); tr.appendChild(td);
          });
          body.appendChild(tr);
        });
      } else {
        // Newest first: the operator's question is almost always about today.
        // The API stays chronological so its contract is unchanged.
        payload.rows.slice().reverse().forEach((row) => {
          const tr = document.createElement('tr');
          const first = document.createElement('td');
          first.textContent = `${row.date} (${row.weekday})`;
          tr.appendChild(first);
          payload.sources.forEach((source) => {
            const td = document.createElement('td');
            td.appendChild(coverageCellNode(row.cells[source] || {}));
            tr.appendChild(td);
          });
          body.appendChild(tr);
        });
      }
      const inventory = payload.legacy_inventory || {};
      const observed = Object.entries(inventory.observed || {})
        .map(([source, value]) => `${COLLECTION_SOURCE_LABELS[source] || source} ${value.first}~${value.last} (${value.dates}일)`)
        .join(' · ');
      $('collection-coverage-badge').textContent = `${payload.start} ~ ${payload.end}`;
      $('coverage-note').textContent = [
        `KST 기준. 근거가 없는 날짜는 미수집, 근거가 있어도 판정할 수 없으면 불명으로 표시합니다.`,
        inventory.complete ? `V0 legacy 인벤토리 완전 · ${observed || '관측 없음'}` : `V0 legacy 인벤토리 불완전 (${inventory.reason || '사유 불명'}) — 부재는 미수집의 근거가 아닙니다.`,
        // Say the numbers, not just the fact. Without them the reader cannot
        // tell how far to narrow the range to turn `unexamined` into a verdict.
        payload.legacy_meta_probed
          ? 'legacy meta.json 확인함'
          : `legacy meta.json 미확인 — 조회 범위 ${payload.requested_span_days}일이 상한 ${payload.legacy_meta_probe_limit_days}일을 넘습니다. 범위를 좁히면 그 날짜들이 판정됩니다.`,
        payload.range_truncated ? '조회 범위가 상한으로 잘렸습니다.' : '',
      ].filter(Boolean).join(' · ');
    }

    function renderCollectionRules(registry) {
      const host = $('collection-rules');
      host.textContent = '';
      registry.rules.forEach((rule) => {
        const block = document.createElement('article');
        block.className = 'rule-block';
        const header = document.createElement('header');
        const title = document.createElement('h3');
        title.textContent = `${rule.version} · ${rule.title}`;
        header.appendChild(title);
        header.appendChild(collectionPill(rule.status === 'active' ? '적용 중' : '대체됨', rule.status === 'active' ? 'collected' : 'unknown'));
        block.appendChild(header);

        const digest = document.createElement('div');
        digest.className = 'mono';
        digest.textContent = `digest ${rule.digest} · registry schema v${rule.registry_schema_version}`;
        block.appendChild(digest);

        [
          rule.summary,
          `적용 기간: ${rule.effective.start || '불명'} ~ ${rule.effective.end || (rule.status === 'active' ? '현재' : '불명')} (${rule.effective.basis})`,
          `manifest schema ${rule.manifest_schema_version ?? '없음'} · ledger schema ${rule.ledger_schema_version ?? '없음'} · source schema ${rule.source_schema_version ?? '없음'}`,
          `capture profile: ${rule.capture_profiles.join(', ') || '없음'}`,
        ].forEach((text) => {
          const line = document.createElement('div');
          line.className = 'work-line';
          line.textContent = text;
          block.appendChild(line);
        });

        if (rule.storage_layout && rule.storage_layout.length) {
          block.appendChild(ruleList('저장 위치', rule.storage_layout));
        }
        if (rule.unknowns && rule.unknowns.length) {
          block.appendChild(ruleList('결손 · 불명 항목', rule.unknowns));
        }
        rule.sources.forEach((source) => {
          const section = document.createElement('div');
          section.className = 'rule-source';
          const heading = document.createElement('h4');
          heading.textContent = `${source.label} — ${DENSITY_LABELS[source.density_kind] || source.density_kind}`;
          section.appendChild(heading);
          const scope = document.createElement('div');
          scope.className = 'work-line';
          scope.textContent = `범위: ${source.scope}`;
          section.appendChild(scope);
          const density = document.createElement('div');
          density.className = 'work-line';
          density.textContent = `밀도: ${source.density}`;
          section.appendChild(density);
          [['포함', source.includes], ['제외', source.excludes], ['알려진 한계', source.known_limitations],
           ['근거', source.evidence], ['불명', source.unknowns]].forEach(([label, values]) => {
            if (values && values.length) section.appendChild(ruleList(label, values));
          });
          block.appendChild(section);
        });
        host.appendChild(block);
      });
      $('collection-registry-badge').textContent = registry.digests_pinned
        ? `append-only 확인됨 · 활성 ${registry.active_version}`
        : '경고: 게시된 규칙의 digest가 일치하지 않습니다';
    }

    function ruleList(label, values) {
      const wrap = document.createElement('div');
      const heading = document.createElement('div');
      heading.className = 'work-line';
      const strong = document.createElement('b');
      strong.textContent = label;
      heading.appendChild(strong);
      wrap.appendChild(heading);
      const list = document.createElement('ul');
      values.forEach((value) => { const item = document.createElement('li'); item.textContent = value; list.appendChild(item); });
      wrap.appendChild(list);
      return wrap;
    }

    function fillCollectionEnvironments(environments) {
      const names = new Set();
      Object.values(environments || {}).forEach((list) => (list || []).forEach((name) => names.add(name)));
      const ordered = [...names].sort();
      if (ordered.join('|') === collectionState.environments.join('|')) return;
      collectionState.environments = ordered;
      const select = $('collection-environment');
      const current = select.value;
      select.textContent = '';
      // Blank means production, not everything: a smoke run must never fill in
      // a gap in the production picture someone is about to make a call on.
      const production = document.createElement('option');
      production.value = ''; production.textContent = '운영 (production)';
      select.appendChild(production);
      const all = document.createElement('option');
      all.value = 'all'; all.textContent = '전체 환경 (시험 포함)';
      select.appendChild(all);
      ordered.filter((name) => name !== 'production').forEach((name) => {
        const option = document.createElement('option');
        option.value = name; option.textContent = name;
        select.appendChild(option);
      });
      select.value = current === 'all' || ordered.includes(current) ? current : '';
    }

    function coverageQuery() {
      const parameters = new URLSearchParams();
      const start = $('coverage-start').value;
      const end = $('coverage-end').value;
      if (start) parameters.set('start', start);
      if (end) parameters.set('end', end);
      parameters.set('group', $('coverage-group').value);
      const environment = $('collection-environment').value;
      if (environment) parameters.set('environment', environment);
      return parameters.toString();
    }

    async function loadCoverage() {
      try {
        const payload = await api(`/api/v1/admin/collection/coverage?${coverageQuery()}`);
        if (!$('coverage-start').value) $('coverage-start').value = payload.start;
        if (!$('coverage-end').value) $('coverage-end').value = payload.end;
        renderCoverage(payload);
        collectionState.coverageAt = payload.generated_at;
        renderFreshness(collectionState.overviewAt, collectionState.coverageAt);
      } catch (error) {
        $('coverage-note').textContent = `커버리지를 불러오지 못했습니다: ${error.message}`;
      }
    }

    async function loadCollection({ quiet = false, coverage = false } = {}) {
      if (collectionState.busy) return;
      collectionState.busy = true;
      try {
        const environment = $('collection-environment').value;
        const query = environment ? `?limit=40&environment=${encodeURIComponent(environment)}` : '?limit=40';
        const overview = await api(`/api/v1/admin/collection/overview${query}`);
        fillCollectionEnvironments(overview.environments);
        renderCollectionCards(overview);
        renderCollectionRuns(overview);
        const scanned = overview.roots.archive_roots || [overview.roots.archive_root];
        $('collection-rule-badge').textContent = scanned.length > 1
          ? `원본 루트 ${scanned.length}곳 · ${overview.roots.archive_root} 외 ${scanned.length - 1}`
          : `원본 루트 ${overview.roots.archive_root}`;
        $('collection-rule-badge').title = scanned.join('\n');
        collectionState.overviewAt = overview.generated_at;
        collectionState.scope = overview.environment_scope || 'production';
        renderFreshness(collectionState.overviewAt, collectionState.coverageAt);
        setCollectionState(`갱신 ${new Date(overview.generated_at).toLocaleTimeString()} · 진행 중 판정 기준 ${Math.round(overview.stale_after_seconds / 60)}분 · 진행 스냅숏 ${overview.roots.progress_available ? '사용 가능' : '미생성'}`);
        if (!collectionState.rulesLoaded) {
          renderCollectionRules(await api('/api/v1/admin/collection/rules'));
          collectionState.rulesLoaded = true;
        }
        if (coverage || !$('coverage-start').value) await loadCoverage();
      } catch (error) {
        if (!quiet) setCollectionState(`수집 현황을 불러오지 못했습니다: ${error.message}`, true);
      } finally {
        collectionState.busy = false;
      }
    }

    function startCollectionPolling() {
      stopCollectionPolling();
      collectionState.timer = setInterval(() => loadCollection({ quiet: true }), COLLECTION_POLL_MS);
    }

    function stopCollectionPolling() {
      if (collectionState.timer) { clearInterval(collectionState.timer); collectionState.timer = null; }
    }

    // The server says when it built the payload. Showing the browser's clock
    // here would claim a freshness the data may not have.
    function renderFreshness(overviewAt, coverageAt) {
      const parts = [];
      if (overviewAt) parts.push(`현황 ${collectionWhen(overviewAt)}`);
      if (coverageAt) parts.push(`커버리지 ${collectionWhen(coverageAt)}`);
      const scope = collectionState.scope === 'all'
        ? '전체 환경 (시험 수집 포함)'
        : `환경 ${collectionState.scope || 'production'}`;
      $('collection-freshness').textContent = parts.length
        ? `${scope} · 서버 응답 생성 시각 — ${parts.join(' · ')} (매니페스트·원장 캐시 1시간, 실행 중 디렉터리 15초)`
        : '';
    }

    async function hardRefreshCollection() {
      const button = $('collection-hard-refresh');
      button.disabled = true;
      try {
        await api('/api/v1/admin/collection/refresh?screen=all', { method: 'POST', body: '{}' });
        await loadCollection({ coverage: true });
        toast('서버 캐시를 비우고 다시 읽었습니다');
      } catch (error) {
        toast(`캐시 비우기에 실패했습니다: ${error.message}`, true);
      } finally {
        button.disabled = false;
      }
    }

    $('collection-refresh').addEventListener('click', () => loadCollection({ coverage: true }));
    $('collection-hard-refresh').addEventListener('click', hardRefreshCollection);
    $('coverage-load').addEventListener('click', () => { loadCoverage(); writeHash(); });
    $('coverage-group').addEventListener('change', () => { loadCoverage(); writeHash(); });
    $('collection-environment').addEventListener('change', () => { loadCollection({ coverage: true }); writeHash(); });

    // ------------------------------------------------------- URL 해시 라우팅
    // Hash-only, so no server route changes and no existing bookmark breaks.
    // A page with no hash behaves exactly as it did before.
    // Derived from the nav itself so a screen added later cannot be silently
    // unroutable. Disabled entries stay out: linking to one would land on a
    // screen the operator cannot reach by clicking.
    const HASH_PAGES = Array.from(document.querySelectorAll('nav button[data-page]'))
      .filter((button) => !button.disabled)
      .map((button) => button.dataset.page);
    const DEFAULT_PAGE = 'work';
    let applyingHash = false;

    function currentPage() {
      const active = document.querySelector('nav button.active');
      return active ? active.dataset.page : DEFAULT_PAGE;
    }

    function hashState() {
      const page = currentPage();
      const parameters = new URLSearchParams();
      if (page === 'collection') {
        const environment = $('collection-environment').value;
        if (environment) parameters.set('environment', environment);
        if ($('coverage-group').value !== 'date') parameters.set('group', $('coverage-group').value);
        if ($('coverage-start').value) parameters.set('start', $('coverage-start').value);
        if ($('coverage-end').value) parameters.set('end', $('coverage-end').value);
      } else if (page === 'work') {
        if (workState.assignee) parameters.set('assigned_to', workState.assignee);
      }
      const query = parameters.toString();
      return `#/${page}${query ? `?${query}` : ''}`;
    }

    function writeHash({ replace = false } = {}) {
      if (applyingHash) return;
      const next = hashState();
      if (next === location.hash) return;
      const url = location.pathname + location.search + next;
      if (replace) history.replaceState(null, '', url); else history.pushState(null, '', url);
    }

    function selectPage(page) {
      document.querySelectorAll('nav button').forEach((node) => node.classList.remove('active'));
      document.querySelectorAll('.page').forEach((node) => node.classList.remove('active'));
      const button = document.querySelector(`nav button[data-page="${page}"]`);
      if (button) button.classList.add('active');
      const section = $(`page-${page}`);
      if (section) section.classList.add('active');
    }

    function applyHash() {
      const raw = location.hash.replace(/^#\/?/, '');
      const [rawPage, rawQuery] = raw.split('?');
      // An unknown or absent hash falls back to the default screen, and the
      // address bar is corrected rather than left saying something untrue.
      const page = HASH_PAGES.includes(rawPage) ? rawPage : DEFAULT_PAGE;
      const parameters = new URLSearchParams(rawQuery || '');
      applyingHash = true;
      try {
        selectPage(page);
        if (page === 'collection') {
          const environment = parameters.get('environment');
          if (environment !== null) $('collection-environment').value = environment;
          const group = parameters.get('group');
          if (group === 'weekday' || group === 'date') $('coverage-group').value = group;
          if (parameters.get('start')) $('coverage-start').value = parameters.get('start');
          if (parameters.get('end')) $('coverage-end').value = parameters.get('end');
        } else if (page === 'work') {
          const assignee = parameters.get('assigned_to');
          if (assignee !== null) { workState.assignee = assignee; $('work-filter-assignee').value = assignee; }
        }
      } finally {
        applyingHash = false;
      }
      // Editing dialogs are deliberately never restored: reviving text someone
      // abandoned would look like their unsaved input had come back.
      closeWorkEditor();
      closeTimeline();
      stopWorkPolling();
      stopCollectionPolling();
      if (page === 'search') { loadSearchCorpus(); }
      if (page === 'org') loadOrg();
      if (page === 'unmapped') loadUnmapped();
      if (page === 'pairs') loadPairs({ reset: true });
      if (page === 'person') { if (!$('person-day').value) $('person-day').value = yesterdayKST(); }
      if (page === 'audit') loadAudit();
      if (page === 'work') { loadWork(); startWorkPolling(); }
      if (page === 'collection') { loadCollection({ coverage: true }); startCollectionPolling(); }
      if (page === 'schedules' && typeof loadSchedules === 'function') loadSchedules();
      if (page === 'server' && typeof loadServerStatus === 'function') loadServerStatus();
      if (!HASH_PAGES.includes(rawPage)) writeHash({ replace: true });
    }

    window.addEventListener('hashchange', applyHash);


    // -------------------------------------------------- 스케줄 · 서버 상태
    function scheduleRow(label, value, title) {
      const row = document.createElement('tr');
      const head = document.createElement('td');
      head.textContent = label;
      head.style.color = 'var(--muted)';
      head.style.whiteSpace = 'nowrap';
      const cell = document.createElement('td');
      cell.textContent = value === null || value === undefined || value === '' ? '불명' : value;
      if (title) cell.title = title;
      row.appendChild(head); row.appendChild(cell);
      return row;
    }

    function scheduleWhen(value) {
      if (!value) return null;
      const moment = new Date(value);
      return Number.isNaN(moment.getTime()) ? null : moment.toLocaleString();
    }

    function renderSchedules(payload) {
      const host = $('schedule-list');
      host.textContent = '';
      (payload.batches || []).forEach((batch) => {
        const block = document.createElement('article');
        block.className = 'rule-block';
        const header = document.createElement('header');
        const title = document.createElement('h3');
        title.textContent = batch.name;
        header.appendChild(title);
        const install = batch.installation || {};
        const tone = install.installed === true
          ? (install.active ? 'collected' : 'partial')
          : (install.installed === false ? 'failed' : 'unknown');
        header.appendChild(collectionPill(install.state_label || '불명', tone));
        block.appendChild(header);

        const table = document.createElement('table');
        table.className = 'audit';
        const body = document.createElement('tbody');
        const next = batch.next_run || {};
        const configured = batch.configured_next_run || {};
        [
          ['목적', batch.purpose],
          ['실행 주체', batch.runner],
          ['실행 명령', batch.command],
          ['systemd unit', `${batch.timer_unit} · ${batch.service_unit}`],
          ['주기', batch.cadence],
          ['실행 시각 · 타임존', batch.schedule_description],
          ['설정상 다음 실행', configured.at
            ? `${scheduleWhen(configured.at)} (${configured.timezone}) · 설정값이며 확정 아님`
            : `불명 — ${configured.reason || '설정을 읽을 수 없습니다'}`],
          ['systemd 다음 실행', next.at
            ? `${scheduleWhen(next.at)} · systemd 기준(확정)`
            : `불명 — ${next.unknown_reason || '사유 불명'}`],
          ['최근 실행', scheduleWhen(batch.last_trigger_at) || '기록 없음 또는 불명'],
          ['최근 결과', batch.last_result
            ? `${batch.last_result}${batch.last_exit_status ? ` (exit ${batch.last_exit_status})` : ''}`
            : '불명'],
          ['활성 상태', install.reason ? `불명 — ${install.reason}` : (install.state_label || '불명')],
          ['대상 범위', batch.scope],
          ['중복 방지', batch.concurrency],
          ['로그', batch.logs],
          ['실패 확인법', batch.failure_check],
          ['상세 설명', batch.detail],
        ].forEach(([label, value]) => body.appendChild(scheduleRow(label, value)));
        table.appendChild(body);
        const scroll = document.createElement('div');
        scroll.className = 'table-scroll';
        scroll.appendChild(table);
        block.appendChild(scroll);
        host.appendChild(block);
      });
      $('schedule-badge').textContent = payload.systemd_readable
        ? `배치 ${(payload.batches || []).length}건 · systemd 조회됨`
        : 'systemd 조회 불가';
      $('schedule-state').textContent = payload.systemd_readable
        ? `갱신 ${new Date(payload.generated_at).toLocaleTimeString()}`
        : `systemd 상태를 읽을 수 없습니다: ${payload.systemd_unavailable_reason || '사유 불명'}. 설치·활성·다음 실행은 불명으로 표시합니다.`;
    }

    function renderBatchRuns(payload) {
      const host = $('batch-runs-list');
      host.textContent = '';
      const runs = payload.runs || [];
      const table = document.createElement('table');
      const head = document.createElement('thead');
      const headRow = document.createElement('tr');
      ['배치', '결과', '시작', '종료', '소요', '경과', 'exit'].forEach((label) => {
        const cell = document.createElement('th');
        cell.textContent = label;
        headRow.appendChild(cell);
      });
      head.appendChild(headRow);
      table.appendChild(head);
      const body = document.createElement('tbody');
      const badOutcomes = new Set(['failed', 'stalled', 'no-state', 'unreadable']);
      runs.forEach((run) => {
        const row = document.createElement('tr');
        const duration = run.duration_seconds != null
          ? `${Math.round(run.duration_seconds / 60)}분` : '';
        const since = run.hours_since != null ? `${run.hours_since}h 전` : '';
        let outcome = run.outcome;
        if (run.outcome === 'running' && run.log_idle_minutes != null) {
          outcome = `running · 로그 ${run.log_idle_minutes}분 전`;
        }
        if (run.overdue) outcome += ' · 지연';
        [run.name, outcome,
         scheduleWhen(run.started_at) || '', scheduleWhen(run.finished_at) || '',
         duration, since,
         run.exit_code != null ? String(run.exit_code) : ''].forEach((value, index) => {
          const cell = document.createElement('td');
          cell.textContent = value == null ? '' : String(value);
          if (index === 1 && (badOutcomes.has(run.outcome) || run.overdue)) {
            cell.style.color = 'var(--danger, #c0392b)';
            cell.style.fontWeight = '600';
          }
          if (run.detail && index === 1) cell.title = run.detail;
          row.appendChild(cell);
        });
        body.appendChild(row);
      });
      table.appendChild(body);
      const scroll = document.createElement('div');
      scroll.className = 'table-scroll';
      scroll.appendChild(table);
      host.appendChild(scroll);
      const counts = payload.counts || {};
      $('batch-runs-badge').textContent =
        `배치 ${counts.total ?? runs.length}건 · 문제 ${counts.failing ?? 0} · 지연 ${counts.overdue ?? 0}`;
      $('batch-runs-state').textContent = (payload.failing || []).length || (payload.overdue || []).length
        ? `확인 필요: ${[...new Set([...(payload.failing || []), ...(payload.overdue || [])])].join(', ')}`
        : '모든 배치가 마지막 실행을 정상 종료했습니다.';
    }

    async function loadBatchRuns() {
      try {
        renderBatchRuns(await api('/api/v1/admin/collection/batch-runs'));
      } catch (error) {
        $('batch-runs-state').textContent = `배치 실행 기록을 불러오지 못했습니다: ${error.message}`;
        $('batch-runs-badge').textContent = '조회 실패';
      }
    }

    // 조직도 · 일자별 · 미확인 계정.
    // 모두 배치가 만든 것을 읽기만 한다. 유일한 예외는 미확인 계정에 주인을
    // 답하는 것인데, 그건 시스템이 물어본 질문에 사람이 답하는 것이다.
    function yesterdayKST() {
      const now = new Date(Date.now() + 9 * 3600 * 1000 - 24 * 3600 * 1000);
      return now.toISOString().slice(0, 10);
    }

    function countTile(label, value, note, accent) {
      const card = document.createElement('div');
      card.className = 'card';
      if (accent) card.style.borderLeft = `2px solid ${accent}`;
      const k = document.createElement('div'); k.className = 'label'; k.textContent = label;
      const v = document.createElement('div'); v.className = 'metric'; v.textContent = value;
      if (accent) v.style.color = accent;
      const n = document.createElement('p'); n.className = 'help'; n.textContent = note || '';
      card.append(k, v, n);
      return card;
    }

    function orgTreeRows(nodes, host, depth) {
      nodes.forEach((node) => {
        const row = document.createElement('div');
        row.style.cssText = `display:flex;justify-content:space-between;gap:10px;padding:7px 9px;`
          + `padding-left:${9 + depth * 18}px;border-radius:7px;font-size:13.5px;`
          + (depth === 0 ? 'color:var(--text);font-weight:600;margin-top:10px' : 'color:var(--muted)');
        const name = document.createElement('span');
        name.textContent = node.name;
        const count = document.createElement('span');
        count.style.cssText = 'font:500 11.5px/1 ui-monospace,monospace;color:#6d7b8e';
        count.textContent = node.people;
        row.append(name, count);
        host.appendChild(row);
        orgTreeRows(node.children || [], host, depth + 1);
      });
    }

    function orgGroups(nodes, host) {
      nodes.forEach((node) => {
        if ((node.members || []).length) {
          const block = document.createElement('div');
          block.style.cssText = 'border:1px solid var(--line-soft,#1e2734);border-radius:10px;'
            + 'padding:12px;margin-top:12px;background:#0e131a';
          const head = document.createElement('div');
          head.className = 'card-header';
          head.style.marginBottom = '6px';
          const title = document.createElement('h2');
          title.style.fontSize = '14px';
          title.textContent = node.path;
          const badge = document.createElement('span');
          badge.className = 'badge';
          badge.textContent = `${node.members.length}명`;
          head.append(title, badge);
          // Banded: 정규직 -> 계약직 · 인턴 under the company, 교수 -> 학생
          // in a lab. The bands and their order come from the server, which
          // is the one place that rule is written; this only titles them.
          const newGrid = () => {
            const grid = document.createElement('div');
            grid.style.cssText = 'display:grid;gap:10px;margin-top:8px;'
              + 'grid-template-columns:repeat(auto-fill,minmax(200px,1fr))';
            return grid;
          };
          const personCard = (person) => {
            const card = document.createElement('button');
            card.className = 'button';
            card.style.cssText = 'text-align:left;display:grid;gap:4px;padding:11px 12px';
            const name = document.createElement('strong');
            name.textContent = person.name;
            const sub = document.createElement('small');
            sub.style.color = 'var(--muted)';
            sub.textContent = [person.nickname, person.employment_type || person.title]
              .filter(Boolean).join(' · ');
            card.append(name, sub);
            // 회사원이면서 학생: the engagement, written on the student's card.
            if (person.company_mark && person.member_group === '학생') {
              const mark = document.createElement('span');
              mark.className = 'badge ok';
              mark.textContent = person.company_mark;
              card.appendChild(mark);
            }
            if (person.status === 'absent_from_sheet') {
              const warn = document.createElement('span');
              warn.className = 'badge wait';
              warn.textContent = '시트에 없음';
              card.appendChild(warn);
            }
            card.addEventListener('click', () => {
              $('person-id').value = person.person_id;
              if (!$('person-day').value) $('person-day').value = yesterdayKST();
              showPage('person');
              runPersonDay();
            });
            return card;
          };
          block.appendChild(head);
          const bands = node.groups || [];
          if (bands.length <= 1) {
            const grid = newGrid();
            node.members.forEach((person) => grid.appendChild(personCard(person)));
            block.appendChild(grid);
          } else {
            bands.forEach((band) => {
              const label = document.createElement('div');
              label.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:10px;'
                + 'padding-top:8px;border-top:1px solid var(--line-soft,#1e2734);'
                + 'font:500 12px/1 system-ui;color:var(--muted,#6d7b8e)';
              const text = document.createElement('span');
              text.textContent = band.name;
              const n = document.createElement('span');
              n.className = 'badge';
              n.textContent = `${band.people}명`;
              label.append(text, n);
              const grid = newGrid();
              node.members
                .filter((person) => person.member_group === band.name)
                .forEach((person) => grid.appendChild(personCard(person)));
              block.append(label, grid);
            });
          }
          host.appendChild(block);
        }
        orgGroups(node.children || [], host);
      });
    }

    async function loadOrg() {
      const line = $('org-state');
      try {
        const chart = await api('/api/v1/admin/org/chart');
        const counts = chart.headcount || {};
        const access = counts.by_access || {};
        const tiles = $('org-counts');
        tiles.textContent = '';
        const describe = (obj) => Object.entries(obj).map(([k, v]) => `${k} ${v}`).join(' · ');
        tiles.appendChild(countTile('고유 인원', counts.people || 0, '사람 수. 방문연구원은 한 번'));
        tiles.appendChild(countTile('접근 기준 합계',
          Object.values(access).reduce((a, b) => a + b, 0), describe(access), 'var(--accent-2)'));
        tiles.appendChild(countTile('소속 구분', Object.keys(counts.by_affiliation || {}).length,
          describe(counts.by_affiliation || {})));
        const unplaced = chart.professors_without_a_lab || [];
        if (unplaced.length) {
          tiles.appendChild(countTile('연구실이 안 붙은 교수님', unplaced.length,
            `${unplaced.join(' · ')} — 학생이 아직 없거나 표기가 다릅니다`, 'var(--warning)'));
        }
        const unmapped = chart.unmapped_accounts || [];
        if (unmapped.length) {
          tiles.appendChild(countTile('주인 없는 계정', unmapped.length,
            unmapped.slice(0, 4).map((a) => `${a.kind} ${a.value}`).join(' · '), 'var(--warning)'));
        }

        const tree = $('org-tree');
        tree.textContent = '';
        orgTreeRows(chart.tree || [], tree, 0);
        const people = $('org-people');
        people.textContent = '';
        orgGroups(chart.tree || [], people);

        $('org-badge').textContent = `${(chart.tree || []).length} 루트`;
        $('org-people-badge').textContent = `${chart.people || 0}명`;
        line.textContent = chart.reason
          ? chart.reason
          : `로스터 관측 ${chart.observed_at ? new Date(chart.observed_at).toLocaleString() : '없음'} 기준`;
      } catch (error) {
        line.textContent = `조직도를 불러오지 못했습니다: ${error.message}`;
        $('org-badge').textContent = '조회 실패';
      }
    }

    async function runPersonDay() {
      const personId = $('person-id').value.trim();
      const day = $('person-day').value;
      const line = $('person-state');
      $('person-head').textContent = '';
      $('person-counts').textContent = '';
      $('person-events').textContent = '';
      if (!personId || !day) { line.textContent = 'person_id 와 날짜가 필요합니다.'; return; }
      line.textContent = '불러오는 중…';
      try {
        const body = await api(`/api/v1/admin/org/digest/${encodeURIComponent(personId)}?day=${day}`);
        if (!body.built) {
          // 만들지 않은 날과 활동이 없던 날은 다른 답이다.
          line.textContent = `${day} 의 다이제스트가 아직 만들어지지 않았습니다 — 활동이 없었다는 뜻이 아닙니다. `
            + `배치가 만들거나 worklog digest --since 로 백필합니다.`;
          $('person-badge').textContent = '미생성';
          return;
        }
        const state = body.state || {};
        const head = $('person-head');
        const title = document.createElement('h2');
        title.textContent = body.name;
        const sub = document.createElement('p');
        sub.className = 'help';
        sub.textContent = [state.nickname, state.title, state.department_raw].filter(Boolean).join(' · ');
        const tags = document.createElement('div');
        tags.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;margin-top:8px';
        [state.affiliation, state.access_level, state.employment_type]
          .filter(Boolean).forEach((value) => {
            const pill = document.createElement('span');
            pill.className = 'badge';
            pill.textContent = value;
            tags.appendChild(pill);
          });
        (body.identities || []).forEach((identity) => {
          const pill = document.createElement('span');
          pill.className = 'badge';
          pill.textContent = `${identity.kind} ${identity.value}`;
          tags.appendChild(pill);
        });
        head.append(title, sub, tags);

        const bySource = (body.counts || {}).by_source || {};
        const tiles = $('person-counts');
        Object.entries(bySource).forEach(([source, count]) => {
          tiles.appendChild(countTile(source, count, ''));
        });
        tiles.appendChild(countTile('합계', body.events_total || 0, '그날 전부'));

        const host = $('person-events');
        const events = body.events || [];
        if (!events.length) {
          const empty = document.createElement('p');
          empty.className = 'help';
          empty.textContent = '이 날은 수집된 활동이 없습니다. 「활동 없음」과 「수집 실패」는 다른 답이고, 이것은 전자입니다.';
          host.appendChild(empty);
        } else {
          const table = document.createElement('table');
          table.className = 'audit';
          const body_ = document.createElement('tbody');
          events.forEach((event) => {
            const row = document.createElement('tr');
            [event.time, event.event_type, event.title || '제목 없음',
             [event.container, event.thread].filter(Boolean).join(' · ')]
              .forEach((value, index) => {
                const cell = document.createElement('td');
                if (index === 2 && !event.title) cell.style.color = '#6d7b8e';
                if (index === 2 && event.permalink) {
                  const link = document.createElement('a');
                  link.href = event.permalink;
                  link.target = '_blank';
                  link.rel = 'noopener';
                  link.textContent = value;
                  cell.appendChild(link);
                } else {
                  cell.textContent = value;
                }
                row.appendChild(cell);
              });
            body_.appendChild(row);
          });
          table.appendChild(body_);
          const scroll = document.createElement('div');
          scroll.className = 'table-scroll';
          scroll.appendChild(table);
          host.appendChild(scroll);
        }
        $('person-badge').textContent = `${body.events_total || 0}건`;
        line.textContent = `${body.day} (KST) · ${new Date(body.generated_at).toLocaleString()} 에 `
          + `${body.generator} 가 생성 · 시간 순, 전부`;
      } catch (error) {
        line.textContent = `불러오지 못했습니다: ${error.message}`;
        $('person-badge').textContent = '조회 실패';
      }
    }

    async function loadUnmapped() {
      const line = $('unmapped-state-line');
      const body = $('unmapped-body');
      body.textContent = '';
      try {
        const payload = await api(`/api/v1/admin/org/unmapped?state=${$('unmapped-state').value}&limit=200`);
        const totals = payload.totals || {};
        $('unmapped-badge').textContent = Object.entries(totals)
          .map(([k, v]) => `${k} ${v.accounts}`).join(' · ') || '없음';
        (payload.accounts || []).forEach((account) => {
          const row = document.createElement('tr');
          [account.kind, account.value, `${account.events}건`,
           account.last_seen_at ? new Date(account.last_seen_at).toLocaleDateString() : '',
           account.state === 'open' ? '' : (account.note || account.state)]
            .forEach((value) => {
              const cell = document.createElement('td');
              cell.textContent = value;
              row.appendChild(cell);
            });
          const actions = document.createElement('td');
          if (account.state === 'open') {
            const input = document.createElement('input');
            input.placeholder = 'person_id';
            input.style.cssText = 'width:150px;padding:6px 8px;margin-right:6px';
            const assign = document.createElement('button');
            assign.className = 'button primary';
            assign.textContent = '이 사람';
            assign.addEventListener('click', () => resolveAccount(account, input.value.trim(), false));
            const ignore = document.createElement('button');
            ignore.className = 'button';
            ignore.style.marginLeft = '6px';
            ignore.textContent = '사람 아님';
            ignore.addEventListener('click', () => resolveAccount(account, null, true));
            actions.append(input, assign, ignore);
          }
          row.appendChild(actions);
          body.appendChild(row);
        });
        line.textContent = (payload.accounts || []).length
          ? '주인을 답하면 별칭으로 저장되어 다시 묻지 않습니다.'
          : '답을 기다리는 계정이 없습니다.';
      } catch (error) {
        line.textContent = `불러오지 못했습니다: ${error.message}`;
        $('unmapped-badge').textContent = '조회 실패';
      }
    }

    async function resolveAccount(account, personId, ignore) {
      const line = $('unmapped-state-line');
      if (!ignore && !personId) { line.textContent = 'person_id 를 넣거나 「사람 아님」을 고르세요.'; return; }
      try {
        await api('/api/v1/admin/org/unmapped/resolve', {
          method: 'POST',
          body: JSON.stringify({ kind: account.kind, value: account.value,
                                 person_id: personId || null, ignore }),
        });
        loadUnmapped();
      } catch (error) {
        line.textContent = `기록하지 못했습니다: ${error.message}`;
      }
    }

    // -------------------------------------------------- 질문·답변 검수
    //
    // HK, 2026-09-21: 네가 페어링을 한것을 가정하되, 나는 수정할 수 있게 하는거지.
    //
    // So each answer renders as its candidates with one already marked, and a
    // click records agreement or a correction. Nothing here rebuilds the
    // candidates: a screen that regenerates what it is showing makes "what did
    // he actually see when he chose this" unanswerable afterwards.
    const pairsState = { offset: 0, loading: false, decided: 0, undecided: 0 };

    async function loadPairs({ reset = false } = {}) {
      const line = $('pairs-state-line');
      const list = $('pairs-list');
      if (pairsState.loading) return;
      pairsState.loading = true;
      if (reset) { pairsState.offset = 0; list.textContent = ''; }
      const person = encodeURIComponent($('pairs-person').value.trim());
      try {
        const payload = await api(`/api/v1/admin/voice/pairs?person_name=${person}`
          + `&state=${$('pairs-state').value}&limit=20&offset=${pairsState.offset}`);
        const counts = payload.counts || {};
        pairsState.decided = counts.decided || 0;
        pairsState.undecided = counts.undecided || 0;
        renderPairCounts();
        const answers = payload.answers || [];
        answers.forEach((answer) => list.appendChild(pairCard(answer)));
        pairsState.offset += answers.length;
        line.textContent = answers.length
          ? `${pairsState.offset}건 표시 중. 제안이 맞으면 「맞음」, 아니면 다른 후보를 고르세요.`
          : (pairsState.offset ? '더 없습니다.' : '검수할 답변이 없습니다.');
        loadPairsHealth(person);
      } catch (error) {
        line.textContent = `불러오지 못했습니다: ${error.message}`;
        $('pairs-badge').textContent = '조회 실패';
      } finally {
        pairsState.loading = false;
      }
    }

    // The proposal rate, next to the queue rather than buried: if it is not
    // moving, the reviewing is data entry. The count of answers carrying no
    // proposal sits beside it for the same reason -- those are the rows he has
    // to review from scratch.
    async function loadPairsHealth(person) {
      const node = $('pairs-health');
      try {
        const [rate, audit] = await Promise.all([
          api(`/api/v1/admin/voice/agreement?person_name=${person}`),
          api(`/api/v1/admin/voice/audit?person_name=${person}&limit=1`),
        ]);
        const decided = rate.decided_answers || 0;
        const parts = [];
        if (rate.agreement_rate === null || rate.agreement_rate === undefined) {
          // A rate over zero decisions is not a low rate, it is an unanswered
          // question, and the screen says so rather than printing 0%.
          parts.push(decided
            ? `${decided}건 결정 · 전부 「해당 없음」이라 제안 정확도는 아직 알 수 없습니다.`
            : '아직 결정한 답변이 없어 제안 정확도는 알 수 없습니다.');
        } else {
          parts.push(`${decided}건 결정 · 제안이 맞았던 비율 `
            + `${Math.round(rate.agreement_rate * 100)}%`
            + ` (수정 ${rate.corrected || 0}, 해당 없음 ${rate.none_of_these || 0})`);
        }
        if (audit.no_proposal) parts.push(`제안이 없는 답변 ${audit.no_proposal}건`);
        node.textContent = parts.join(' · ');
      } catch (_) {
        // The health line is not the screen. If it cannot be read the queue
        // still works, and a broken number is worse than no number.
        node.textContent = '';
      }
    }

    function pairCard(answer) {
      const card = document.createElement('article');
      card.className = 'pair';
      card.dataset.answer = answer.answer_ledger_id;

      const head = document.createElement('div');
      head.className = 'pair-head';
      const when = document.createElement('span');
      when.className = 'pair-when';
      when.textContent = `${new Date(answer.answer_at).toLocaleString()} · ${answer.channel}`;
      head.appendChild(when);
      if (answer.permalink) {
        const link = document.createElement('a');
        link.href = answer.permalink;
        link.target = '_blank';
        link.rel = 'noreferrer';
        link.className = 'pair-link';
        link.textContent = '슬랙에서 보기';
        head.appendChild(link);
      }
      card.appendChild(head);

      // HK, 2026-09-23: 질문/대답 순서로 해주면 좋겠어.
      //
      // Which is how the exchange actually happened, and it is not only
      // cosmetic: reading the answer first makes every candidate look
      // plausible, because a question can be invented to fit an answer after
      // the fact. Question first, then what he said, asks the right question
      // -- "was this the one he was answering" rather than "could this have
      // been".
      const questions = document.createElement('div');
      questions.className = 'pair-questions';
      const questionLabel = document.createElement('div');
      questionLabel.className = 'pair-label';
      questionLabel.textContent = '질문 후보';
      questions.appendChild(questionLabel);
      card.appendChild(questions);

      (answer.candidates || []).forEach((candidate) => {
        const row = document.createElement('div');
        row.className = 'pair-candidate'
          + (candidate.chosen ? ' chosen' : (candidate.proposed ? ' proposed' : ''));
        const mark = document.createElement('span');
        mark.className = 'pair-mark';
        mark.textContent = candidate.chosen ? '선택' : (candidate.proposed ? '제안' : '');
        const text = document.createElement('span');
        text.className = 'pair-text';
        text.textContent = candidate.text;
        const basis = document.createElement('span');
        basis.className = 'pair-basis';
        // The route is shown because it is the thing his corrections teach:
        // which one to trust when they disagree.
        basis.textContent = `${candidate.basis} ${candidate.score}`;
        const pick = document.createElement('button');
        pick.className = `button${candidate.proposed ? ' primary' : ''}`;
        pick.textContent = candidate.proposed ? '맞음' : '이게 질문';
        pick.addEventListener('click', () => choosePair(answer, candidate.pair_id));
        row.append(mark, text, basis, pick);
        questions.appendChild(row);
      });

      const none = document.createElement('button');
      none.className = 'button pair-none';
      none.textContent = '해당 없음';
      none.title = '후보 중에 답하고 있던 질문이 없습니다. 이 답변은 큐에서 빠집니다.';
      none.addEventListener('click', () => choosePair(answer, null));
      questions.appendChild(none);

      const answerLabel = document.createElement('div');
      answerLabel.className = 'pair-label';
      answerLabel.textContent = 'HK 의 답변';
      card.appendChild(answerLabel);
      const said = document.createElement('p');
      said.className = 'pair-answer';
      said.textContent = answer.answer_text;
      card.appendChild(said);

      if (answer.decided) card.classList.add('decided');
      return card;
    }

    // HK, 2026-09-23: 대답을 선택하고 있는데 제대로 되고 있는거야?
    //
    // A fair question, and the screen was not answering it: the row vanished
    // and a toast said so for two seconds. Vanishing is what a dropped click
    // would look like too. So the running count of what has been decided is
    // updated on every decision and stays on screen -- if it does not move,
    // nothing was recorded, and that is visible without asking.
    async function choosePair(answer, pairId) {
      const card = document.querySelector(`.pair[data-answer="${answer.answer_ledger_id}"]`);
      try {
        await api('/api/v1/admin/voice/pairs/choose', {
          method: 'POST',
          body: JSON.stringify({ answer_ledger_id: answer.answer_ledger_id, pair_id: pairId }),
        });
        // Removed from the list rather than re-fetched: re-fetching would
        // renumber everything under his cursor mid-review.
        if (card) card.remove();
        pairsState.decided += 1;
        if (pairsState.undecided > 0) pairsState.undecided -= 1;
        renderPairCounts();
        toast(pairId ? '기록했습니다.' : '해당 없음으로 기록했습니다.');
        loadPairsHealth(encodeURIComponent($('pairs-person').value.trim()));
      } catch (error) {
        toast(`기록하지 못했습니다: ${error.message}`, true);
      }
    }

    function renderPairCounts() {
      $('pairs-badge').textContent =
        `고른 것 ${pairsState.decided} · 남은 것 ${pairsState.undecided}`;
    }


    async function loadSchedules() {
      loadBatchRuns();
      try {
        renderSchedules(await api('/api/v1/admin/schedules'));
      } catch (error) {
        $('schedule-state').textContent = `스케줄 정보를 불러오지 못했습니다: ${error.message}`;
        $('schedule-badge').textContent = '조회 실패';
      }
    }

    async function loadServerStatus() {
      const body = $('server-runtime-body');
      body.textContent = '';
      const rows = [];
      try {
        const overview = await api('/api/v1/admin/collection/overview?limit=1');
        rows.push(['외부 원본 루트', overview.roots.archive_root]);
        // Naming every archive that was read, so a month held in a backfill
        // root is not reported as never collected, and a root nobody scanned
        // is visible by its absence from this list.
        (overview.roots.archive_roots || []).slice(1).forEach((root, index) => {
          rows.push([`백필 아카이브 ${index + 1}`, root]);
        });
        rows.push(['원장 staging 루트', overview.roots.ledger_root]);
        rows.push(['레거시 루트', overview.roots.legacy_root]);
        rows.push(['진행 스냅숏', overview.roots.progress_available ? `사용 가능 · ${overview.roots.progress_root}` : `미생성 · ${overview.roots.progress_root}`]);
        rows.push(['수집 환경', Object.entries(overview.environments || {}).map(([source, list]) => `${source}: ${list.join(', ')}`).join(' · ') || '불명']);
      } catch (error) {
        rows.push(['수집 런타임', `불명 — ${error.message}`]);
      }
      try {
        const response = await fetch('/healthz', { credentials: 'same-origin' });
        rows.push(['서비스 데이터베이스', response.ok ? '응답함 (healthz ok)' : `응답하지 않음 (HTTP ${response.status})`]);
      } catch (error) {
        rows.push(['서비스 데이터베이스', '불명 — healthz에 도달하지 못했습니다']);
      }
      const configured = Object.entries(state.secrets || {}).filter(([, ok]) => ok).map(([name]) => name);
      rows.push(['구성된 인증정보', configured.length ? configured.join(', ') : '없음']);
      rows.push(['타임존 설정', state.settings.timezone || '불명']);
      rows.forEach(([label, value]) => body.appendChild(scheduleRow(label, value)));
      $('server-runtime-badge').textContent = `확인 ${new Date().toLocaleTimeString()}`;
    }

    $('nav').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-page]');
      if (!button || button.disabled) return;
      // The hash is the single source of truth for which screen is shown, so a
      // click, a reload, the back button and a shared link all take one path.
      const target = `#/${button.dataset.page}`;
      if (location.hash === target) applyHash(); else location.hash = target;
    });

    $('search-run').addEventListener('click', runSearch);
    $('person-run').addEventListener('click', runPersonDay);
    $('person-day').addEventListener('change', () => { if ($('person-id').value.trim()) runPersonDay(); });
    $('pairs-reload').addEventListener('click', () => loadPairs({ reset: true }));
    $('pairs-more').addEventListener('click', () => loadPairs());
    $('pairs-state').addEventListener('change', () => loadPairs({ reset: true }));
    $('pairs-person').addEventListener('change', () => loadPairs({ reset: true }));
    $('unmapped-reload').addEventListener('click', loadUnmapped);
    $('unmapped-state').addEventListener('change', loadUnmapped);
    $('search-q').addEventListener('keydown', (event) => { if (event.key === 'Enter') runSearch(); });

    $('logout').addEventListener('click', async () => {
      stopWorkPolling();
      stopCollectionPolling();
      try { await api('/api/v1/admin/logout', { method: 'POST', body: '{}' }); } catch (_) {}
      state.csrf = null; const session = await api('/api/v1/admin/session'); showAuth(session);
    });

    initialize();
