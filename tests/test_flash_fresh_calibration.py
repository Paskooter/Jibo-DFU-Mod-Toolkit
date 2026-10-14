"""Two flash choices, safe defaults and offline transport acceptance."""
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
import jibo_dfu as dfu
import jibo_tui as tui
from gui_fakes import FakeApi, FakeHosted, HostedImage

KEEP = "Keep this Jibo's calibration (recommended)"
UNSAFE = "Flash without calibration (unsafe)"
PHOENIX = "This image connects Jibo to Phoenix (jibo.io). After flashing he starts at the setup screens; finish setup at jibo.io."


@pytest.mark.parametrize("phoenix", [False, True])
def test_local_flash_has_exactly_two_enabled_choices_and_keep_default(phoenix):
    prompts = []
    def select(title, options, **kw):
        prompts.append((tui._normalise_options(options), kw))
        return None
    with patch.object(tui, 'select_option', select):
        result = tui._run_local_update(FakeApi(), Path('image.tar.bz2'), phoenix_marked=phoenix)
    choices, kw = prompts[0]
    assert [(i.key, i.label, i.enabled) for i in choices] == [
        ('keep-calibration', KEEP, True), ('without-calibration', UNSAFE, True)]
    assert kw['initial'] == 'keep-calibration'
    assert choices[1].tone == 'danger'
    assert result['status'] == 'cancelled'


def test_hosted_flash_has_same_two_choices():
    items = tui.hosted_policy_items(FakeHosted(), HostedImage('phx-2', '2.0.0'))
    assert [(i.key, i.label, i.enabled) for i in items] == [
        ('keep-calibration', KEEP, True), ('without-calibration', UNSAFE, True)]


@pytest.mark.parametrize('command', ['flash-update', 'flash-hosted-update'])
def test_cli_help_has_no_obsolete_policy_and_defaults_to_keep(command):
    stream = io.StringIO()
    with redirect_stdout(stream), pytest.raises(SystemExit) as exit:
        dfu.main([command, '--help'])
    assert exit.value.code == 0
    text = stream.getvalue()
    assert '--preserve-var' not in text
    assert '--fresh-var' not in text
    assert '--keep-calibration' in text and '--without-calibration' in text
    assert 'default' in text.lower()


@pytest.mark.parametrize('hosted', [False, True])
def test_unsafe_decline_defaults_cancel_and_never_calls_flash(hosted):
    api = FakeApi()
    prompts = []
    answers = iter(['without-calibration', None])
    def select(title, options, **kw):
        prompts.append((title, tui._normalise_options(options), kw))
        return next(answers)
    with patch.object(tui, 'select_option', select):
        if hosted:
            result = tui._install_hosted(api, FakeHosted(), HostedImage('phx-2','2.0.0'), 'p', '1-1')
        else:
            result = tui._run_local_update(api, 'p', '1-1')
    assert result['status'] == 'cancelled' and not api.calls and not api.writes
    assert len(prompts) == 2 and prompts[-1][2]['initial'] == 'cancel'
    assert [i.key for i in prompts[-1][1]] == ['cancel', 'confirm']


@pytest.mark.parametrize('marked', [False, True])
def test_phoenix_line_only_for_authenticated_phoenix_plan(marked):
    plan = {'package': 'p'}
    if marked:
        plan['phoenix'] = {'id': 'phx-2'}
    assert (PHOENIX in tui._hosted_confirmation_details(plan)) is marked


def test_backend_resume_refuses_without_touching_usb_or_package():
    with patch.object(dfu, 'devices') as usb, patch.object(dfu.updates, 'validate_package') as pkg:
        with pytest.raises(dfu.DfuError, match='cannot currently resume'):
            dfu.flash_update('p', resume_from='record.json')
    usb.assert_not_called()
    pkg.assert_not_called()


def test_backend_unsafe_requires_dedicated_ack_before_any_usb():
    with patch.object(dfu, 'devices') as usb, patch.object(dfu.updates, 'validate_package') as pkg:
        with pytest.raises(dfu.DfuError, match='explicit unsafe'):
            dfu.flash_update('p', var_policy='without-calibration', confirmation='FLASH UPDATE')
    usb.assert_not_called()
    pkg.assert_not_called()


@pytest.mark.parametrize('policy', ['keep-calibration', 'without-calibration'])
@pytest.mark.parametrize('phoenix', [False, True])
@pytest.mark.parametrize('valid_backup', [True, False])
def test_real_ext4_fresh_backup_then_write_exact_policy_bytes(tmp_path, policy, phoenix, valid_backup, include_partition_backups=False):
    import json
    from contextlib import ExitStack
    from types import SimpleNamespace
    import jibo_images as images
    from calibration_support import make_image, FILES, PATHS, IDENTITY, POINTERCAL, FACTORY_RECORD
    from test_updates import compact_set
    entries = {PATHS[k]: FILES[k] for k in ('left','right','inter_camera','imu','audio')}
    entries.update({'/jibo/identity.json': IDENTITY, '/jibo/credentials.json': b'{"region":"api"}',
                    '/etc/wpa_supplicant.conf': b'network={ssid="Test"}', '/jibo/user.txt': b'private'})
    backup_img = make_image(tmp_path, 'backup.img', entries)
    stock_entries = {PATHS[k]: FILES[k] for k in ('left', 'right', 'inter_camera')}
    stock_entries['/jibo/mode.json'] = b'{"mode":"oobe"}'
    stock = make_image(tmp_path, 'stock.img', stock_entries)
    tag = 'serial-sha256:' + 'a' * 64
    digest = images.sha256_file(backup_img)
    manifest = tmp_path / 'backup-manifest.json'
    manifest.write_text(json.dumps({'kind':'jibo-var-backup', 'device_tag':tag, 'sha256':digest}))
    backup = {'image':str(backup_img), 'manifest':str(manifest), 'sha256':digest}
    prepared = {name: stock for name in dfu.UPDATE_ORDER}
    metadata = None
    if phoenix:
        metadata = {'id':'test', 'profile':'test', 'strategy':'compact-prefix-v1',
                    'base_release':'13.0.0', 'components':{}, '_metadata_sha256':'a'*64,
                    'members':[{'role':role, 'size':stock.stat().st_size, 'sha256':images.sha256_file(stock)}
                               for role in ('rootfs','services','skills','var')]}
    package = SimpleNamespace(source=tmp_path/'package', version='13.0.0', phoenix=metadata)
    events, written = [], {}
    def save(*args, **kw):
        assert kw == {'refresh':True}
        events.append('backup')
        return backup
    def transfer(argv, **kw):
        name = argv[argv.index('-a')+1]
        assert events[0] == 'backup'
        events.append('write-'+name)
        written[name] = Path(argv[argv.index('-D')+1]).read_bytes()
    def upload(util, port, name, size, dest, capacity=None):
        import hashlib
        return hashlib.sha256(written[name]).hexdigest()
    capacities = {name:stock.stat().st_size for name in dfu.UPDATE_ORDER}
    if include_partition_backups:
        for name, key, raw in [('rootfsA','touchscreen',POINTERCAL),('skills','factory_record',FACTORY_RECORD)]:
            directory = tmp_path/'saved'/('partition-backup-'+name)
            directory.mkdir(parents=True)
            saved = make_image(directory,'partition.img',{PATHS[key]:raw})
            (directory/'backup-manifest.json').write_text(json.dumps({
                'kind':'jibo-partition-backup', 'partition':name, 'device_tag':tag,
                'size_bytes':saved.stat().st_size, 'sha256':images.sha256_file(saved)}))
    if not valid_backup:
        with backup_img.open('r+b') as damaged:
            damaged.seek(4096)
            damaged.write(b'SYNTHETIC CORRUPTION')
    with ExitStack() as s:
        for owner, name, kw in [
            (dfu, 'EXPECTED_VAR_SIZE', {'new':stock.stat().st_size}),
            (dfu, 'BACKUP_ROOT', {'new':tmp_path/'saved'}),
            (dfu.updates, 'validate_package', {'return_value':package}),
            (dfu.updates, 'prepare_compact_images', {'return_value':compact_set(prepared)}),
            (dfu, 'devices', {'return_value':[{'port':'1-1','state':'dfu'}]}),
            (dfu, '_dfu_context', {'return_value':('1-1',list(capacities)+[dfu.MARKER],tag,'')}),
            (dfu, '_read_gpt_capacities', {'return_value':capacities}),
            (dfu, '_hosted_space_precheck', {'return_value':None}),
            (dfu, '_package_content_sha256', {'return_value':'b'*64}),
            (dfu, 'backup_var', {'side_effect':save}),
            (dfu, 'run_with_progress', {'side_effect':transfer}),
            (dfu, '_upload_partition', {'side_effect':upload}),
            (dfu, 'run', {'return_value':''})]:
            s.enter_context(patch.object(owner, name, **kw))
        # Exercise old flash semantics in controls instead of failing merely on its API.
        import inspect
        kwargs = dict(port='1-1', dfu_util='offline', out=tmp_path/'op', confirmation='FLASH UPDATE')
        if 'var_policy' in inspect.signature(dfu.flash_update).parameters:
            args = ('package',)
            kwargs.update(var_policy=policy, unsafe_without_calibration=policy=='without-calibration')
        else:
            args = ('package', False)
            kwargs.update(fresh_with_calibration=policy=='keep-calibration')
        with redirect_stdout(io.StringIO()):
            if not valid_backup:
                with pytest.raises(dfu.DfuError, match='backup does not match'):
                    dfu.flash_update(*args, **kwargs)
                assert not written and events == ['backup']
                return
            result = dfu.flash_update(*args, **kwargs)
    assert list(written) == list(dfu.UPDATE_ORDER)
    assert images.sha256_file(backup_img) == digest
    if policy == 'without-calibration':
        assert written['var'] == stock.read_bytes()
    else:
        candidate = tmp_path/'captured.img'
        candidate.write_bytes(written['var'])
        for path in list(entries)[:5] + ['/jibo/identity.json']:
            assert images._extract(candidate,path,tmp_path/'read') == entries[path]
        for path in ('/jibo/credentials.json','/etc/wpa_supplicant.conf','/jibo/user.txt'):
            assert images._extract(candidate,path,tmp_path/'absent',optional=True) is None
        assert images._extract(candidate,'/jibo/mode.json',tmp_path/'mode') == b'{"mode":"oobe"}'
    assert result['var_policy'] == policy
    if include_partition_backups:
        for name, key, raw in [('rootfsA','touchscreen',POINTERCAL),('rootfsB','touchscreen',POINTERCAL),('skills','factory_record',FACTORY_RECORD)]:
            captured = tmp_path/(name+'-captured.img')
            captured.write_bytes(written[name])
            actual = images._extract(captured,PATHS[key],tmp_path/'item-read',optional=True)
            assert actual == (raw if policy=='keep-calibration' else None)


@pytest.mark.parametrize('document', ['metadata', 'manifest'])
def test_wire_accepts_exact_new_policies_and_refuses_old(document):
    import jibo_flash_contract as contract
    schema = contract.CONTRACT['fields'][document]['var_policies']
    contract.check_schema(['keep-calibration', 'without-calibration'], schema)
    for obsolete in (['preserve-var'], ['keep-calibration'], ['without-calibration'],
                     ['without-calibration','keep-calibration'], ['keep-calibration','keep-calibration']):
        with pytest.raises(ValueError):
            contract.check_schema(obsolete, schema)


@pytest.mark.skipif(__import__('os').environ.get('JIBO_GUI_TEST_DISPLAY') != '1', reason='real window required')
def test_real_window_flash_options_show_details_and_red_unsafe(tmp_path):
    import jibo_gui as gui
    import jibo_gui_core as core
    root = gui.create_root()
    c = core.GuiController(FakeApi(), runner=core.sync_runner, preferences_path=tmp_path/'prefs')
    w = gui.ToolkitWindow(root, c)
    try:
        prompt = core.Prompt('select', 'Choose calibration handling', initial='keep-calibration',
                             items=tui.hosted_policy_items(FakeHosted(), HostedImage('test','1.0.0')))
        w.show_prompt(prompt)
        root.update()
        rows = w.controls['options']
        assert list(rows) == ['keep-calibration','without-calibration']
        assert rows['keep-calibration'].selected
        unsafe = rows['without-calibration']
        assert unsafe.label.cget('fg') == gui.P['danger_text']
        assert unsafe.reason is not None and 'A backup of his current data' in unsafe.reason.cget('text')
        assert rows['keep-calibration'].reason is not None
        assert 'Wi-Fi, pairing and settings start fresh' in rows['keep-calibration'].reason.cget('text')
    finally:
        w.close()



def test_tui_enabled_choice_detail_is_visible_and_unsafe_red():
    class Screen:
        def __init__(self): self.lines=[]
        def keypad(self,*a): pass
        def erase(self): self.lines=[]
        def getmaxyx(self): return (24,80)
        def addnstr(self,y,x,text,length,attr=0): self.lines.append((y,text[:length],attr))
        def refresh(self): pass
        def getch(self): return 27
    screen=Screen()
    unsafe_detail="He gets the image's generic settings instead of his own calibration and identity, so his camera, motion and hearing may not work properly. A backup of his current data is saved first."
    choice=tui.MenuItem('unsafe',UNSAFE,reason=unsafe_detail,tone='danger')
    with patch.object(tui,'_start_screen'), patch.object(tui.curses,'init_pair') as color, \
         patch.object(tui.curses,'color_pair',return_value=8192):
        assert tui._select_session(screen,'Choose calibration handling',[choice],initial='unsafe') is None
    visible=' '.join(text.strip() for _,text,_ in screen.lines)
    assert unsafe_detail in visible
    assert any(UNSAFE in text and attr & 8192 for _,text,attr in screen.lines)
    assert all(0 <= y < 24 for y,_,_ in screen.lines)



def test_without_calibration_warning_does_not_promise_setup_without_identity():
    item = tui.hosted_policy_items(FakeHosted(), HostedImage('test','1.0.0'))[1]
    assert 'identity' in item.reason and 'setup may not work' in item.reason
    assert 'restored from a backup' in item.reason



def test_image_preparation_has_no_preserve_partition_switch():
    import inspect
    import jibo_updates
    assert 'preserve_var' not in inspect.signature(jibo_updates.prepare_images).parameters
    assert 'preserve_var' not in inspect.signature(jibo_updates.prepare_compact_images).parameters



def test_cli_default_calls_keep_calibration():
    with patch.object(dfu,'tool',return_value='offline'), patch.object(dfu,'flash_update',return_value={'status':'cancelled'}) as flash, redirect_stdout(io.StringIO()):
        assert dfu.main(['flash-update','package','--yes']) == 0
    assert flash.call_args.args[1] == 'keep-calibration'
    assert flash.call_args.kwargs['unsafe_without_calibration'] is False


@pytest.mark.parametrize('command', ['flash-update','flash-hosted-update'])
def test_cli_unsafe_needs_flag_and_typed_confirmation_not_blanket_yes(command):
    argv = [command,'package'] if command=='flash-update' else [command,'--image','latest']
    if command=='flash-update': argv += ['--yes']
    argv += ['--without-calibration']
    with patch.object(dfu,'devices') as usb, patch.object(dfu,'flash_update') as flash, \
         patch.object(dfu.hosted,'download_image') as download:
        assert dfu.main(argv) == 1
    usb.assert_not_called()
    flash.assert_not_called()
    download.assert_not_called()


@pytest.mark.parametrize('command', ['flash-update','flash-hosted-update'])
def test_cli_typed_unsafe_sets_the_dedicated_ack(command):
    argv = [command,'package'] if command=='flash-update' else [command,'--image','test']
    argv += ['--without-calibration','--confirm','FLASH UPDATE']
    target = 'flash_update' if command=='flash-update' else 'flash_hosted_update'
    with patch.object(dfu,'tool',return_value='offline'), patch.object(dfu,target,return_value={'status':'cancelled'}) as flash, \
         patch.object(dfu.hosted,'download_image',return_value='package'), redirect_stdout(io.StringIO()):
        assert dfu.main(argv) == 0
    assert flash.call_args.kwargs['unsafe_without_calibration'] is True
    policy = flash.call_args.args[1] if command=='flash-update' else flash.call_args.kwargs['var_policy']
    assert policy == 'without-calibration'



@pytest.mark.skipif(__import__('os').environ.get('JIBO_GUI_TEST_DISPLAY') != '1', reason='real window required')
@pytest.mark.parametrize('geometry', ['920x640','1080x720'])
def test_real_window_unsafe_second_step_defaults_cancel_and_writes_nothing(tmp_path,geometry):
    import time
    import jibo_gui as gui
    import jibo_gui_core as core
    api = FakeApi()
    c = core.GuiController(api, preferences_path=tmp_path/'prefs')
    root = gui.create_root()
    w = gui.ToolkitWindow(root,c)
    root.geometry(geometry)
    def pump(predicate):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            root.update()
            if predicate(): return
            time.sleep(0.01)
        raise AssertionError('GUI prompt did not arrive: '+w.page_name)
    try:
        root.update()
        root.focus_force()
        w.open_action('flash-update')
        pump(lambda:w.prompt is not None)
        w.controls['primary'].invoke()
        pump(lambda:w.prompt is not None and w.prompt.title=='Choose calibration handling')
        assert w.controls['selected']() == 'keep-calibration'
        w.controls['choose']('without-calibration')
        w.controls['primary'].invoke()
        pump(lambda:w.prompt is not None and w.prompt.kind=='confirm')
        assert w.prompt.title == 'Confirm flash without calibration (unsafe)'
        assert w.prompt.initial == 'cancel'
        assert root.focus_get() == w.controls['cancel']
        assert w.controls['primary'].cget('style') == 'Danger.TButton'
        assert not api.calls and not api.writes
        w.controls['cancel'].invoke()
        pump(lambda:not c.busy)
        assert not api.calls and not api.writes
    finally:
        c.shutdown()
        pump(lambda:not c.busy)
        w.close()



from test_hosted_catalog import HostedFixture


@pytest.mark.parametrize('policy',['keep-calibration','without-calibration'])
def test_hosted_partition_calibration_authenticates_stock_then_verifies_personalized_bytes(tmp_path,policy):
    test_real_ext4_fresh_backup_then_write_exact_policy_bytes(
        tmp_path, policy, True, True, include_partition_backups=True)


class SignedPolicyAdmissionTests(HostedFixture):
    def metadata(self, policies):
        import base64
        import json
        import tarfile
        import jibo_flash_contract as contract
        data = self.server.files[self.row['url'].removeprefix(self.server.origin)]
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:bz2') as archive:
            member = next(m for m in archive if m.name.endswith('/phoenix-flash.json'))
            envelope = json.loads(archive.extractfile(member).read())
        payload = json.loads(base64.b64decode(envelope['payload']))
        payload['var_policies'] = policies
        return contract.verify_envelope(self.server.envelope(payload), 'metadata')

    def test_signed_new_policy_metadata_is_accepted(self):
        import jibo_flash_contract as contract
        metadata = contract.validate_metadata(self.metadata(['keep-calibration','without-calibration']))
        self.assertEqual(metadata['var_policies'], ['keep-calibration','without-calibration'])

    def test_workspace_reserves_both_root_copies_and_skills_calibration_candidate(self):
        import jibo_flash_contract as contract
        metadata = self.metadata(['keep-calibration','without-calibration'])
        sizes = {member['role']:member['size'] for member in metadata['members']}
        capacity = 524288000
        minimum = 2*sum(sizes.values())+3*capacity+max(max(sizes.values()),capacity)+1024**3
        minimum += 2*sizes['rootfs']+sizes['skills']
        self.assertGreaterEqual(contract.workspace_bytes(metadata,capacity), minimum)

    def test_signed_obsolete_policy_metadata_is_refused(self):
        import jibo_flash_contract as contract
        with self.assertRaises(contract.ContractError):
            contract.validate_metadata(self.metadata(['preserve-var']))
