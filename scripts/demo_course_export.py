"""Create synthetic, offline course data for documentation screenshots.

Run with an empty, disposable MONASH_KIT_HOME, then:
  monash export FIT1043 --format both --to ./exports --no-ask
Never point this script at a real account's data directory.
"""
import os
from pathlib import Path

from monash_study_kit import edlib, lessons, moodlelib
from monash_study_kit.paths import HOME, FILES_DIR, ED_FILES_DIR

if not os.environ.get('MONASH_KIT_HOME') or any(HOME.glob('**/*')):
    raise SystemExit('Set MONASH_KIT_HOME to an empty disposable directory.')
HOME.mkdir(parents=True, exist_ok=True)
m = moodlelib.db_connect()
m.execute("INSERT INTO courses(id,code,folder,fullname,tracked,synced_at) VALUES(7,'FIT1043','FIT1043 Demo','Synthetic course materials',1,'2026-11-03')")
for i, (name, body) in enumerate([
    ('Page.md', '# Course overview\n\nSynthetic Moodle Page.\n'),
    ('Book.md', '# Course handbook\n\n## Week 1\nSynthetic Book chapter.\n'),
    ('Assignment.md', '# Assignment instructions\n\nSynthetic brief; no student submissions.\n'),
], 1):
    path = Path('FIT1043 Demo/Week 01') / name
    target = FILES_DIR / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding='utf-8')
    m.execute("INSERT INTO files(course_id,source,cmid,path,size,section,title,synced_at) VALUES(7,?,?,?,?,?,?,?)",
              (f'https://example.org/demo/{i}',i,str(path),target.stat().st_size,'Week 01',name,'2026-11-03'))
m.execute("INSERT INTO files(course_id,source,path,section,title,synced_at) VALUES(7,'https://example.org/unavailable','FIT1043 Demo/Week 01/Unavailable.pdf','Week 01','Unavailable attachment','2026-11-03')")
m.execute("INSERT INTO links(course_id,folder,section,title,url,synced_at) VALUES(7,'Week 01','Week 01','Google Slides (not authorized in demo)','https://docs.google.com/presentation/d/synthetic-example/edit','2026-11-03')")
m.commit()
m.close()
e=edlib.db_connect()
lessons.ensure_lessons_schema(e)
e.execute("INSERT INTO courses(id,code,name,tracked) VALUES(24100,'FIT1043 S2 2026','Synthetic course materials',1,'2026-11-03')")
e.execute("INSERT INTO modules(id,course_id,name) VALUES(1,24100,'Week 1')")
e.execute("INSERT INTO lessons(id,course_id,module_id,idx,title,status) VALUES(11,24100,1,0,'Introduction','unattempted')")
path = ED_FILES_DIR / 'FIT1043/Week 01/Lesson.md'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text('# Lesson introduction\n\nSynthetic Ed lesson document.\n', encoding='utf-8')
e.execute("INSERT INTO lesson_files(id,lesson_id,course_id,type,title,local_path,idx) VALUES(101,11,24100,'document','Lesson introduction','FIT1043/Week 01/Lesson.md',0)")
e.commit()
e.close()
print('Synthetic fixture ready. No login or network requests were made.')
