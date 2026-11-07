"""Exercise the real admin renderer inside the existing offline Chromium fixture."""

def check_key_status_ui(page):
    original = page.evaluate('keysCache')
    try:
        page.evaluate('''() => {
          const base=keysCache[0], now=Date.now()/1000;
          keysCache=[
            {...base,id:101,name:'permanent-on',enabled:true,expires_at:null},
            {...base,id:102,name:'future-on',enabled:true,expires_at:now+3600},
            {...base,id:103,name:'expired-on',enabled:true,expires_at:now-60},
            {...base,id:104,name:'expired-off',enabled:false,expires_at:Math.floor(now)},
            {...base,id:105,name:'future-off',enabled:false,expires_at:now+3600},
          ];
          renderKeys();
        }''')
        status = page.locator('#keyStatusFilter')
        assert status.locator('option[value="expired"]').count() == 1, 'missing expired status filter'
        rows = page.locator('#keysTable tbody tr')
        assert rows.count() == 5
        assert rows.filter(has_text='expired-on').locator('.tag').inner_text() == '已过期'
        assert rows.filter(has_text='expired-off').locator('.tag').inner_text() == '已过期'
        assert page.locator('#keyNavCount').inner_text() == '3'
        assert '3 个未过期' in page.locator('#keyCount').inner_text()
        assert '2 个已过期' in page.locator('#keyCount').inner_text()
        for state, names in [('on', ['permanent-on', 'future-on']),
                             ('off', ['future-off']),
                             ('expired', ['expired-on', 'expired-off'])]:
            status.select_option(state)
            assert rows.count() == len(names)
            for name in names:
                assert rows.filter(has_text=name).count() == 1
        page.locator('#keySearch').fill('expired-on')
        assert rows.count() == 1
        page.get_by_role('button', name='编辑', exact=True).click()
        assert page.locator('#modalTitle').inner_text() == '编辑 Key'
        assert '到期' in page.locator('#expiryLabel').inner_text()
        page.evaluate('closeModal()')
        page.locator('#keySearch').fill('')
        for width in (1280, 640, 390, 320):
            page.set_viewport_size({'width': width, 'height': 900})
            assert rows.count() == 2
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # Renewed metadata must move a key out of the expired filter immediately.
        page.evaluate('''() => {
          keysCache.find(k=>k.id===103).expires_at=Date.now()/1000+3600;
          renderKeys();
        }''')
        assert rows.count() == 1
        assert rows.filter(has_text='expired-off').count() == 1
        status.select_option('on')
        assert rows.count() == 3
        assert rows.filter(has_text='expired-on').locator('.tag').inner_text() == '已启用'
        assert page.locator('#keyNavCount').inner_text() == '4'
    finally:
        page.evaluate('(original) => { keysCache=original; }', original)
        page.locator('#keySearch').fill('')
        page.locator('#keyStatusFilter').select_option('all')
        page.evaluate('renderKeys()')
        page.set_viewport_size({'width': 1280, 'height': 900})
