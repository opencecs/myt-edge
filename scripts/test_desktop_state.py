#!/usr/bin/env python3
"""Regression tests for offline MYT Desktop state migration.

Run: python3 scripts/test_desktop_state.py

Only temporary regular files/directories are touched. Tests extract the
production shell helpers; all block probing and mount/umount operations are
simulated. SQLite fixtures are real databases, including committed WAL data.
No root privileges, real partitions, board connection or external packages
are required. The shell helpers are executed with /bin/dash (Debian /bin/sh).
"""
import hashlib,json,os,pathlib,shutil,sqlite3,stat,subprocess,tempfile,unittest
# Locate the checkout from this file so the test works from any cwd.
REPO=pathlib.Path(__file__).resolve().parents[1]
SOURCE=(REPO/'rootfs/overlay/usr/libexec/y4-data-provision').read_text()
FUNCS=SOURCE.split('# BEGIN desktop-state helpers',1)[1].split('# END desktop-state helpers',1)[0]
FUNCS=FUNCS.replace('desktop_current=/var/lib/myt_desktop','desktop_current=$TEST_CURRENT')
PREFIX='''set -eu
fail() { echo "FAIL: $*" >&2; exit 1; }
desktop_tmp=
previous_mounted=no
data_mounted=no
'''
CLEANUP=SOURCE[SOURCE.index('cleanup() {'):SOURCE.index('# BEGIN desktop-state helpers')]
MOCKS='''find_previous_slot() { previous_device=$TEST_OLD_DEVICE; }
mount() {
    test "$*" = "-t ext4 -o ro,noload $TEST_OLD_DEVICE $state/previous-slot" || exit 91
    echo "mount ro,noload" >> "$TEST_LOG"
    rmdir "$6"
    ln -s "$TEST_OLD" "$6"
}
umount() {
    test "$1" = "$state/previous-slot" || exit 92
    echo "umount" >> "$TEST_LOG"
    rm "$1"
    mkdir "$1"
}
'''
def digest_tree(p):
    d={}
    if not p.exists():return d
    for f in sorted(p.rglob('*')):
        key=str(f.relative_to(p))
        if f.is_symlink(): d[key]=('link',os.readlink(f))
        elif f.is_file():d[key]=('file',hashlib.sha256(f.read_bytes()).hexdigest(),stat.S_IMODE(f.stat().st_mode))
        else:d[key]=('dir',stat.S_IMODE(f.stat().st_mode))
    return d
def database(folder,value='sample'):
    folder.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(folder/'nashy.db') as c:
        c.execute('CREATE TABLE sample(value TEXT)')
        c.execute('INSERT INTO sample VALUES(?)',(value,))
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='desktop-test-')
        self.p=pathlib.Path(self.tmp.name)
        self.current=self.p/'current';self.current.mkdir()
        self.data=self.p/'data';self.data.mkdir()
        self.dest=self.data/'myt_desktop'
        self.old=self.p/'old';self.old.mkdir()
        self.state=self.p/'run';self.state.mkdir()
        self.oldstate=self.old/'var/lib/myt_desktop'
        self.env=dict(os.environ,TEST_CURRENT=str(self.current),TEST_OLD=str(self.old),
            TEST_OLD_DEVICE='',TEST_LOG=str(self.p/'mount.log'),
            state=str(self.state),mountpoint=str(self.data))
    def tearDown(self): self.tmp.cleanup()
    def run_migrate(self,ok=True,extra=''):
        code=PREFIX+FUNCS+MOCKS+CLEANUP+extra+'\ndesktop_state_init\n'
        r=subprocess.run(['/bin/dash','-c',code],env=self.env,text=True,capture_output=True)
        self.assertEqual(r.returncode==0,ok,r.stdout+r.stderr)
        self.assertFalse(list(self.data.glob('.myt-desktop-new.*')),r.stderr)
        return r
    def old_system(self,value='old'):
        (self.old/'etc').mkdir()
        (self.old/'etc/debian_version').write_text('12')
        (self.old/'etc/y4-release.json').write_text('{"rootfs":"20261113-r202","kernel":"6.6.89-cix-build-generic"}')
        svc=self.old/'usr/lib/systemd/system';svc.mkdir(parents=True)
        (svc/'myt_desktop.service').write_text('[Service]\nExecStart=/usr/bin/myt_desktop\n')
        database(self.oldstate,value)
        self.env['TEST_OLD_DEVICE']='/mock/previous'
    def test_existing_persistent_preserved(self):
        database(self.dest,'keep');database(self.current,'ignore')
        before=digest_tree(self.dest);self.run_migrate()
        self.assertEqual(digest_tree(self.dest),before)
        self.assertFalse((self.p/'mount.log').exists())
    def test_initialized_empty_preserved(self):
        self.dest.mkdir();(self.dest/'.y4-state-v1').write_text('y4-myt-desktop-state v1\n')
        before=digest_tree(self.dest);self.run_migrate()
        self.assertEqual(digest_tree(self.dest),before)
    def test_current_db_and_all_state(self):
        database(self.current,'current')
        (self.current/'.hidden').write_bytes(b'secret-fixture')
        (self.current/'nashy.db-journal').write_bytes(b'offline-journal-fixture')
        (self.current/'preinstall').mkdir()
        (self.current/'asset-link').symlink_to('preinstall')
        before=digest_tree(self.current);self.run_migrate()
        actual=digest_tree(self.dest);actual.pop('.y4-state-v1')
        self.assertEqual(actual,before)
        self.assertEqual(digest_tree(self.current),before)
    def test_previous_slot_ro_migration(self):
        self.old_system()
        before=digest_tree(self.old)
        self.run_migrate()
        self.assertEqual(digest_tree(self.old),before)
        self.assertEqual((self.p/'mount.log').read_text(),'mount ro,noload\numount\n')
        self.assertIn('previous-slot',(self.dest/'.y4-state-v1').read_text())
        with sqlite3.connect(self.dest/'nashy.db') as c:
            self.assertEqual(c.execute('select * from sample').fetchall(),[('old',)])
            self.assertEqual(c.execute('pragma quick_check').fetchone()[0],'ok')
    def test_previous_wal_commit_preserved(self):
        self.old_system();(self.oldstate/'nashy.db').unlink()
        scratch=self.p/'writer';scratch.mkdir()
        c=sqlite3.connect(scratch/'nashy.db')
        c.execute('pragma journal_mode=wal');c.execute('pragma wal_autocheckpoint=0')
        c.execute('create table sample(value text)');c.execute("insert into sample values('committed-wal')");c.commit()
        for f in scratch.iterdir():shutil.copy2(f,self.oldstate/f.name)
        c.close()
        before=digest_tree(self.old)
        self.run_migrate()
        self.assertEqual(digest_tree(self.old),before)
        with sqlite3.connect(self.dest/'nashy.db') as c:
            self.assertEqual(c.execute('select * from sample').fetchall(),[('committed-wal',)])
            self.assertEqual(c.execute('pragma quick_check').fetchone()[0],'ok')
    def test_empty_install(self):
        self.current.rmdir();self.run_migrate()
        self.assertTrue((self.dest/'.y4-state-v1').is_file())
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode),0o755)
    def test_seed_without_db(self):
        (self.current/'preinstall').mkdir()
        (self.current/'preinstall/fixture').write_text('asset')
        self.run_migrate()
        self.assertEqual((self.dest/'preinstall/fixture').read_text(),'asset')
    def test_invalid_persistent_not_overwritten(self):
        self.dest.mkdir();(self.dest/'nashy.db').write_text('bad')
        before=digest_tree(self.dest);self.run_migrate(False)
        self.assertEqual(digest_tree(self.dest),before)
    def test_destination_symlink_rejected(self):
        self.dest.symlink_to(self.current)
        self.run_migrate(False);self.assertTrue(self.dest.is_symlink())
    def test_invalid_current_rejected(self):
        (self.current/'nashy.db').write_text('bad')
        self.run_migrate(False);self.assertFalse(self.dest.exists())
    def test_invalid_previous_rejected(self):
        self.old_system();(self.oldstate/'nashy.db').write_text('bad')
        self.run_migrate(False);self.assertFalse(self.dest.exists())
    def test_unrecognized_previous_rejected(self):
        self.old_system();(self.old/'etc/y4-release.json').write_text('{}')
        self.run_migrate(False);self.assertFalse(self.dest.exists())
    def test_two_sources_ambiguous(self):
        self.old_system();(self.current/'asset').write_text('new')
        self.run_migrate(False);self.assertFalse(self.dest.exists())
    def test_previous_files_without_db_rejected(self):
        self.old_system();(self.oldstate/'nashy.db').unlink()
        (self.oldstate/'asset').write_text('retain')
        self.run_migrate(False);self.assertFalse(self.dest.exists())
    def test_copy_failure_has_no_published_state(self):
        database(self.current)
        self.run_migrate(False,'cp() { return 73; }')
        self.assertFalse(self.dest.exists())
        self.run_migrate()
        self.assertTrue((self.dest/'nashy.db').exists())
    def test_publish_failure_can_retry(self):
        database(self.current);self.dest.mkdir()
        self.run_migrate(False,'mv() { return 74; }')
        self.assertFalse(self.dest.exists())
        self.run_migrate()
    def test_repeat_does_not_replace(self):
        database(self.current);self.run_migrate()
        before=digest_tree(self.dest)
        (self.current/'nashy.db').write_text('now-invalid-source')
        self.run_migrate()
        self.assertEqual(digest_tree(self.dest),before)

    def test_failed_migration_isolated_and_gate_cleared(self):
        database(self.current)
        (self.state/'myt-desktop-ready').write_text('stale')
        code=PREFIX+FUNCS+MOCKS+CLEANUP+"""
data_mounted=yes
cp() { return 73; }
desktop_state_prepare
test "$data_mounted" = yes || exit 93
data_mounted=no
printf 'OTHER_DATA_READY=yes\n'
"""
        r=subprocess.run(['/bin/dash','-c',code],env=self.env,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('OTHER_DATA_READY=yes',r.stdout)
        self.assertIn('other data mounts remain available',r.stderr)
        self.assertFalse((self.state/'myt-desktop-ready').exists())
        self.assertFalse(self.dest.exists())
        self.assertFalse(list(self.data.glob('.myt-desktop-new.*')))
        self.assertFalse((self.p/'mount.log').exists())
    def test_success_sets_startup_gate(self):
        database(self.dest)
        code=PREFIX+FUNCS+MOCKS+CLEANUP+'\ndesktop_state_prepare\n'
        r=subprocess.run(['/bin/dash','-c',code],env=self.env,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual((self.state/'myt-desktop-ready').read_text(),'ready\n')
    def test_previous_failure_unmounts_without_data_mount(self):
        self.old_system();(self.oldstate/'nashy.db').write_text('invalid')
        code=PREFIX+FUNCS+MOCKS+CLEANUP+'\ndata_mounted=yes\ndesktop_state_prepare\ntest "$data_mounted" = yes || exit 93\ndata_mounted=no\necho OTHER_DATA_READY=yes\n'
        r=subprocess.run(['/bin/dash','-c',code],env=self.env,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('OTHER_DATA_READY=yes',r.stdout)
        self.assertEqual((self.p/'mount.log').read_text(),'mount ro,noload\numount\n')
        self.assertFalse((self.state/'myt-desktop-ready').exists())


    def test_valid_marker_does_not_hide_invalid_db(self):
        self.dest.mkdir()
        (self.dest/'.y4-state-v1').write_text('y4-myt-desktop-state v1\n')
        (self.dest/'nashy.db').write_text('invalid')
        self.run_migrate(False)
    def test_invalid_marker_rejected(self):
        self.dest.mkdir();(self.dest/'.y4-state-v1').write_text('invalid')
        self.run_migrate(False)
    def test_valid_marker_does_not_allow_db_symlink(self):
        database(self.current)
        self.dest.mkdir()
        (self.dest/'.y4-state-v1').write_text('y4-myt-desktop-state v1\n')
        (self.dest/'nashy.db').symlink_to(self.current/'nashy.db')
        self.run_migrate(False)

class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='geometry-test-')
        self.p=pathlib.Path(self.tmp.name);self.disk=self.p/'disk';self.disk.mkdir()
        self.a=self.disk/'fakep2';self.a.mkdir();self.b=self.disk/'fakep3';self.b.mkdir()
        for d,start in [(self.a,522*2048),(self.b,(522+16384)*2048)]:
            (d/'start').write_text(str(start));(d/'size').write_text(str(16384*2048))
        self.env=dict(os.environ,MIB='1048576',partition_sys=str(self.a),disk_sys=str(self.disk),
            disk_devname='fake',root_device='/dev/fakep2',data_device='/dev/fakep4',
            TEST_FS_TYPE='ext4',TEST_FS_STATE='clean',TEST_FS_FEATURES='has_journal')
    def tearDown(self):self.tmp.cleanup()
    def run_geo(self,ok=True):
        mocks=r'''
desktop_fake_block() { test "$1" = /dev/fakep2 || test "$1" = /dev/fakep3; }
readlink() { echo "$2"; }
blkid() { echo "$TEST_FS_TYPE"; }
dumpe2fs() { printf 'Filesystem state: %s\nFilesystem features: %s\n' "$TEST_FS_STATE" "$TEST_FS_FEATURES"; }
find_previous_slot
printf 'SELECTED=%s\n' "$previous_device"
'''
        r=subprocess.run(['/bin/dash','-c',PREFIX+FUNCS.replace('[ -b "$previous_device" ]', 'desktop_fake_block "$previous_device"')+mocks],env=self.env,text=True,capture_output=True)
        self.assertEqual(r.returncode==0,ok,r.stdout+r.stderr)
        if not ok: self.assertIn("FAIL:",r.stderr)
        return r.stdout
    def test_a_selects_b(self):self.assertIn('SELECTED=/dev/fakep3',self.run_geo())
    def test_b_selects_a(self):
        self.env.update(partition_sys=str(self.b),root_device='/dev/fakep3')
        self.assertIn('SELECTED=/dev/fakep2',self.run_geo())
    def test_unexpected_active_geometry_rejected(self):
        (self.a/'start').write_text('0');self.run_geo(False)
    def test_wrong_active_size_rejected(self):
        (self.a/'size').write_text('4096');self.run_geo(False)
    def test_wrong_previous_size_rejected(self):
        (self.b/'size').write_text('4096');self.run_geo(False)
    def test_missing_previous_rejected(self):
        shutil.rmtree(self.b);self.run_geo(False)
    def test_alias_active_rejected(self):
        self.env['root_device']='/dev/fakep3';self.run_geo(False)
    def test_dirty_previous_rejected(self):
        self.env['TEST_FS_STATE']='not clean';self.run_geo(False)
    def test_needs_recovery_rejected(self):
        self.env['TEST_FS_FEATURES']='has_journal needs_recovery';self.run_geo(False)
    def test_blank_previous_not_mounted(self):
        self.env['TEST_FS_TYPE']='';self.assertIn('SELECTED=\n',self.run_geo())
if __name__=='__main__':unittest.main(verbosity=2)
