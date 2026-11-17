"""Real HTTP against disposable SQLite; no deployment data is opened."""
from client import *

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shift-native-')
        self.db = Path(self.tmp.name) / 'test.db'
        seed = subprocess.run([str(BINARY), '--init', '--db', str(self.db)], capture_output=True, text=True, check=True)
        self.credentials = json.loads(seed.stdout)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.log = open(Path(self.tmp.name) / 'server.log', 'w+')
        self.server = subprocess.Popen([str(BINARY), '--db', str(self.db), '--port', str(self.port), '--web', str(ROOT / 'web')], stdout=self.log, stderr=self.log)
        self.admin = Client(self.port)
        self.addCleanup(self.cleanup)
        for _ in range(100):
            try:
                if self.admin.request('GET', '/healthz')[0] == 200: break
            except OSError: pass
            time.sleep(.02)
        else:
            self.log.seek(0)
            self.fail(self.log.read())
        self.assertEqual(self.admin.request('POST', '/api/login', self.credentials)[0], 200)
        self.assertEqual(self.admin.request('POST', '/api/password', {'old_password': self.credentials['password'], 'new_password': 'Manager-test-password-17'})[0], 200)
        self.worker = self.new_worker('worker-one', 'משתתף ראשון')
        # Use Israel's current calendar date, including near UTC midnight.
        from zoneinfo import ZoneInfo
        self.today = dt.datetime.now(ZoneInfo('Asia/Jerusalem')).date()
        self.day = self.today
        while self.day.weekday() not in (6,0,1,2,3): self.day += dt.timedelta(days=1)
        self.date = self.day.isoformat()
        self.week = (self.day - dt.timedelta(days=(self.day.weekday()+1)%7)).isoformat()

    def cleanup(self):
        self.server.terminate()
        self.server.wait(timeout=5)
        self.log.seek(0)
        output=self.log.read()
        self.assertNotIn("ERROR: AddressSanitizer",output)
        self.assertNotIn("runtime error:",output)
        self.log.close()
        self.tmp.cleanup()

    def new_worker(self, login, name):
        response = self.admin.request('POST', '/api/admin/users', {'login':login,'first_name':name,'last_name':'בדיקה','gender':'other','role':'member','password':'Worker-test-password-17'})
        self.assertEqual(response[0],200,response[1])
        client = Client(self.port)
        self.assertEqual(client.request('POST','/api/login',{'login':login,'password':'Worker-test-password-17'})[0],200)
        return client

    def select(self, client=None, period='09_13', date=None):
        return (client or self.worker).request('POST','/api/shifts',{'date':date or self.date,'period':period})

    def test_replacement_and_cancellation_preserve_history(self):
        self.assertEqual(self.select()[0],200)
        self.assertEqual(self.select()[0],200)
        self.assertEqual(self.select(period='full')[0],200)
        self.assertEqual(self.worker.request('DELETE','/api/shifts',{'date':self.date})[0],200)
        with sqlite3.connect(self.db) as db:
            rows=db.execute('SELECT period,cancellation_reason FROM shifts ORDER BY id').fetchall()
        self.assertEqual(rows,[('09_13','replaced'),('full','worker_cancelled')])

    def test_manager_sees_everyone_worker_sees_only_self(self):
        other=self.new_worker('worker-two','משתתף שני')
        self.assertEqual(self.select()[0],200)
        self.assertEqual(self.select(other,'12_16')[0],200)
        admin=self.admin.request('GET','/api/week?start='+self.week)[1]
        own=self.worker.request('GET','/api/week?start='+self.week+'&worker_id=999')[1]
        self.assertEqual(len(admin['shifts']),2)
        self.assertEqual(len(own['shifts']),1)
        self.assertEqual([p['code'] for p in admin['periods']],['09_13','10_14','11_15','12_16','full'])
        self.assertEqual(admin['periods'][-1]['end'],'16:30')

    def test_closure_cancels_and_reopening_does_not_restore(self):
        self.assertEqual(self.select()[0],200)
        data={'date':self.date,'reason':'המרכז סגור','confirm':True}
        self.assertEqual(self.admin.request('POST','/api/admin/closures',data)[0],200)
        self.assertEqual(self.select(period='full')[0],409)
        self.assertEqual(self.admin.request('DELETE','/api/admin/closures',{'date':self.date})[0],200)
        view=self.admin.request('GET','/api/week?start='+self.week)[1]
        self.assertEqual(view['shifts'],[])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT cancellation_reason,cancellation_note FROM shifts').fetchone(),('date_blocked','המרכז סגור'))
        self.assertEqual(self.select(period='10_14')[0],200)

    def test_roles_origin_and_csrf_are_enforced(self):
        self.assertEqual(Client(self.port).request('GET','/api/week?start='+self.week)[0],401)
        self.assertEqual(self.select(self.admin)[0],403)
        self.assertEqual(self.worker.request('POST','/api/admin/closures',{'date':self.date,'confirm':True})[0],403)
        self.assertEqual(self.worker.request('POST','/api/shifts',{'date':self.date,'period':'full'},csrf=False)[0],403)
        self.assertEqual(self.worker.request('POST','/api/shifts',{'date':self.date,'period':'full'},origin='https://wrong.example')[0],403)

    def test_hour_choices_horizon_and_weekends(self):
        for period in ['09_13','10_14','11_15','12_16','full']:
            self.assertEqual(self.select(period=period)[0],200)
        for period in ['morning','afternoon','09_14','home']:
            self.assertEqual(self.select(period=period)[0],400)
        self.assertEqual(self.select(date=(self.today-dt.timedelta(days=1)).isoformat())[0],400)
        self.assertEqual(self.select(date=(self.today+dt.timedelta(days=29)).isoformat())[0],400)
        friday=self.today+dt.timedelta(days=(4-self.today.weekday())%7)
        self.assertEqual(self.select(date=friday.isoformat())[0],400)

    def test_desk_overlap_adjacency_and_one_reservation_per_day(self):
        date=(self.day+dt.timedelta(days=7)).isoformat()
        other=self.new_worker('desk-worker','משתתף נוסף')
        payload={'date':date,'desk_id':1,'start_time':'09:00','end_time':'10:00'}
        self.assertEqual(self.worker.request('POST','/api/desk-reservations',payload)[0],200)
        self.assertEqual(other.request('POST','/api/desk-reservations',payload)[0],409)
        payload.update(start_time='10:00',end_time='11:00')
        self.assertEqual(other.request('POST','/api/desk-reservations',payload)[0],200)
        payload.update(desk_id=2,start_time='12:00',end_time='13:00')
        self.assertEqual(self.worker.request('POST','/api/desk-reservations',payload)[0],409)
        self.assertEqual(self.admin.request('POST','/api/desk-reservations',payload)[0],403)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shifts').fetchone()[0],0)
        self.assertEqual(self.admin.request('POST','/api/admin/closures',{'date':date,'confirm':True})[0],200)
        self.assertEqual(self.worker.request('POST','/api/desk-reservations',payload)[0],409)
        self.assertEqual(self.admin.request('DELETE','/api/admin/closures',{'date':date})[0],200)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM desk_reservations WHERE cancelled_at IS NULL').fetchone()[0],0)

    def test_concurrent_workers_cannot_double_book_desk(self):
        other=self.new_worker('racing-worker','משתתף נוסף')
        payload={'date':(self.day+dt.timedelta(days=7)).isoformat(),'desk_id':1,'start_time':'09:00','end_time':'11:00'}
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            responses=list(pool.map(lambda c:c.request('POST','/api/desk-reservations',payload),[self.worker,other]))
        self.assertEqual(sorted(r[0] for r in responses),[200,409])

    def test_account_password_is_always_literal_and_session_rotates(self):
        literal='$2y$04$abcdefghijklmnopqrstuuY1cr1HM7UyJZpS4YUJOmn.HmrqKhvsO'
        result=self.worker.request('POST','/api/password',{'old_password':'Worker-test-password-17','new_password':literal})
        self.assertEqual(result[0],200,result[1])
        client=Client(self.port)
        self.assertEqual(client.request('POST','/api/login',{'login':'worker-one','password':literal})[0],200)
        with sqlite3.connect(self.db) as db:
            stored=db.execute("SELECT password FROM users WHERE login='worker-one'").fetchone()[0]
        self.assertNotEqual(stored,literal)
        self.assertTrue(stored.startswith('$argon2id$'))


    def worker_id(self):
        with sqlite3.connect(self.db) as db:
            return db.execute("SELECT id FROM users WHERE login='worker-one'").fetchone()[0]

    def test_deactivate_revokes_sessions_keeps_completed_and_reactivate_does_not_restore(self):
        future=(self.day+dt.timedelta(days=7)).isoformat()
        self.assertEqual(self.select(date=future)[0],200)
        uid=self.worker_id()
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO shifts(worker_id,shift_date,period) VALUES(?,?,'full')",(uid,(self.today-dt.timedelta(days=7)).isoformat()))
        payload={'id':uid,'confirm':True}
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/delete',payload)[0],409)
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/deactivate',payload)[0],200)
        self.assertEqual(self.worker.request('GET','/api/profile')[0],401)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shifts WHERE cancelled_at IS NULL').fetchone()[0],1)
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/reactivate',payload)[0],200)
        self.assertEqual(self.worker.request('POST','/api/login',{'login':'worker-one','password':'Worker-test-password-17'})[0],200)
        self.assertEqual(self.worker.request('GET','/api/week?start='+self.week)[1]['shifts'],[])
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/deactivate',payload)[0],200)
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/delete',payload)[0],200)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shifts WHERE worker_id=?',(uid,)).fetchone()[0],0)

    def test_reset_requires_confirmation_and_revokes_old_session(self):
        payload={'id':self.worker_id(),'password':'Replacement-password-17'}
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/reset-password',payload)[0],400)
        payload['password_confirmation']=payload['password']
        self.assertEqual(self.admin.request('POST','/api/admin/accounts/reset-password',payload)[0],200)
        self.assertEqual(self.worker.request('GET','/api/profile')[0],401)
        self.assertEqual(self.worker.request('POST','/api/login',{'login':'worker-one','password':payload['password']})[0],200)

    def test_receipts_report_and_informational_holiday(self):
        import csv, io
        month=self.today.isoformat()[:7]
        payload={'month':month,'has_receipts':True}
        self.assertEqual(self.worker.request('POST','/api/receipts',payload)[0],200)
        self.assertEqual(self.admin.request('POST','/api/receipts',payload)[0],403)
        self.assertEqual(self.admin.request('POST','/api/admin/events',{'date':self.date,'title':'חג','kind':'holiday'})[0],200)
        self.assertEqual(self.select()[0],200)
        result=self.admin.request('POST','/api/admin/report',{'from':self.today.isoformat(),'to':self.today.isoformat(),'month':month})
        self.assertEqual(result[0],200,result[1])
        self.assertTrue(result[1].startswith('\ufeff'))
        rows=list(csv.reader(io.StringIO(result[1].lstrip('\ufeff'))))
        self.assertEqual(rows[1][1], '1' if self.day==self.today else '0')
        self.assertEqual(rows[1][3],'יש')

    def signup(self, login, route='worker'):
        return Client(self.port).request('POST','/api/signup/'+route,{'login':login,'first_name':'חדש','last_name':'בדיקה','gender':'other','password':'Signup-password-17','password_confirmation':'Signup-password-17','role':'admin'})

    def test_signup_closed_fixed_role_frozen_cohort_and_one_use_review(self):
        self.assertEqual(self.signup('first-join')[0],403)
        self.assertEqual(self.admin.request('POST','/api/admin/signup-settings',{'role':'member','enabled':True})[0],200)
        self.assertEqual(self.signup('first-join')[0],200)
        review=self.admin.request('POST','/api/admin/signup-review',{'action':'approve'})[1]
        self.assertEqual((review['workers'],review['managers']),(1,0))
        self.assertEqual(self.signup('later-join')[0],200)
        result=self.admin.request('POST','/api/admin/signup-process',{'token':review['token']})
        self.assertEqual(result[0],200,result[1])
        self.assertEqual(result[1]['processed'],1)
        self.assertEqual(self.admin.request('POST','/api/admin/signup-process',{'token':review['token']})[0],409)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT role FROM users WHERE login='first-join'").fetchone()[0],'member')
            self.assertEqual(db.execute('SELECT login FROM pending_signups').fetchall(),[('later-join',)])
        self.assertEqual(Client(self.port).request('POST','/api/login',{'login':'first-join','password':'Signup-password-17'})[0],200)

    def test_itzik_hours_clarification_confirmation_and_manager_schedule(self):
        week=(self.day+dt.timedelta(days=7))
        week=(week-dt.timedelta(days=(week.weekday()+1)%7)).isoformat()
        for hours,period in [('מ-9 עד 13','09_13'),('מ-10 עד 14','10_14'),('מ-11 עד 15','11_15'),('מ-12 עד 16','12_16'),('יום מלא','full')]:
            result=self.worker.request('POST','/api/wolfe',{'text':'תרשום אותי ביום ראשון '+hours,'week_start':week})
            self.assertEqual(result[0],200,result[1])
            self.assertEqual(result[1].get('proposal',{}).get('period'),period,result[1])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shifts').fetchone()[0],0)
        vague=self.worker.request('POST','/api/wolfe',{'text':'תרשום אותי ביום ראשון בבוקר','week_start':week})
        self.assertEqual(vague[0],200,vague[1])
        self.assertIsNone(vague[1]['proposal'])
        view=self.admin.request('POST','/api/wolfe',{'text':'תראה לי את השבוע','week_start':week})
        self.assertEqual(view[1]['action'],'show_schedule',view[1])
        self.assertEqual(self.admin.request('POST','/api/wolfe',{'text':'תרשום אותי ביום ראשון יום מלא','week_start':week})[0],403)

    def test_avatar_private_raster_limits_and_deletion(self):
        import base64, struct, zlib
        def png(width):
            def chunk(kind,data): return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
            return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00'+b'\x00'*3*width))+chunk(b'IEND',b'')
        for value in [b'<svg onload="alert(1)"></svg>',png(2049)]:
            self.assertEqual(self.worker.request('POST','/api/avatar',{'base64':base64.b64encode(value).decode()})[0],400)
        self.assertEqual(self.worker.request('POST','/api/avatar',{'base64':base64.b64encode(png(1)).decode()})[0],200)
        url='/api/avatar?id='+str(self.worker_id())
        self.assertEqual(Client(self.port).request('GET',url)[0],401)
        r=self.admin.request('GET',url)
        self.assertEqual(r[0],200)
        self.assertEqual(r[2]['content-type'],'image/png')
        self.assertEqual(self.worker.request('DELETE','/api/avatar',{})[0],200)
        self.assertEqual(self.admin.request('GET',url)[0],404)

    @unittest.skipUnless(__import__('sys').platform.startswith('linux'),'Legacy bcrypt migration uses Linux libxcrypt')
    def test_imported_bcrypt_is_verified_and_upgraded(self):
        # PHP-generated bcrypt for literal Migration-password-17, committed as a fixture only.
        legacy='$2y$04$Ck6UtRRJjpaPuKmTTesAgOtTcHmrjPBJx5tU6T1xD62.qKP.tlA7u'
        with sqlite3.connect(self.db) as db: db.execute("UPDATE users SET password=? WHERE login='worker-one'",(legacy,))
        c=Client(self.port)
        self.assertEqual(c.request('POST','/api/login',{'login':'worker-one','password':'Migration-password-17'})[0],200)
        with sqlite3.connect(self.db) as db:
            self.assertTrue(db.execute("SELECT password FROM users WHERE login='worker-one'").fetchone()[0].startswith('$argon2id$'))

    def test_backup_restore_retains_schedule_and_revokes_sessions(self):
        self.assertEqual(self.select()[0],200)
        backup=Path(self.tmp.name)/'backup.db'
        result=subprocess.run([str(BINARY),'--db',str(self.db),'--backup',str(backup)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        target=Path(self.tmp.name)/'restored.db'
        subprocess.run([str(BINARY),'--init','--db',str(target)],capture_output=True,check=True)
        result=subprocess.run([str(BINARY),'--db',str(target),'--restore',str(backup)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        with sqlite3.connect(target) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM shifts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

if __name__=='__main__': unittest.main()
