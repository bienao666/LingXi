"""150 distinct Chinese regression questions. These are fixtures, not measured results."""
import json
from pathlib import Path


def build():
    cases=[]
    def add(group, fact, query, expected, forbidden=(), action=None):
        cases.append({'id':f'M{len(cases)+1:03}', 'group':group, 'facts':fact if isinstance(fact,list) else [fact],
                      'query':query, 'expected':list(expected), 'forbidden':list(forbidden), 'action':action})
    rows=[
        ('我叫林舟。','我叫什么名字？','林舟'),('我喜欢喝乌龙茶。','我偏好哪种茶？','乌龙'),
        ('我希望你回答简短一点。','回答长度按什么偏好？','简短'),('我不喜欢被连续追问。','你和我聊天应该避免什么？','追问'),
        ('我习惯晚上九点阅读。','我一般几点阅读？','九点'),('我平时使用简体中文。','默认和我用哪种中文？','简体'),
        ('我喜欢清淡的食物。','我喜欢什么口味？','清淡'),('我不喜欢香菜。','我不喜欢什么配菜？','香菜'),
        ('我从事后端开发。','我的工作方向是什么？','后端'),('我主要使用Python。','我主要用什么编程语言？','Python'),
        ('我喜欢徒步。','我喜欢什么户外活动？','徒步'),('我养了一只叫团团的猫。','我的猫叫什么？','团团'),
        ('我希望你称呼我阿舟。','你应该怎么称呼我？','阿舟'),('我周末常去游泳。','我周末常做什么运动？','游泳'),
        ('我喜欢先看结论再看解释。','回答结构按我的偏好怎么安排？','结论'),('我用北京时间安排日程。','我的日程时区是什么？','北京'),
        ('我不喝含酒精的饮品。','给我推荐饮品要注意什么？','酒精'),('我喜欢爵士乐。','我偏好什么音乐？','爵士'),
        ('我希望你直接指出方案的问题。','评审方案时我希望你怎样表达？','直接'),('我更喜欢Markdown格式。','报告用什么格式？','Markdown'),
        ('我习惯先写测试再写业务代码。','我的编码习惯是什么？','测试'),('我喜欢深色界面。','我偏好什么界面主题？','深色'),
        ('我喜欢用清单整理事情。','整理事情用什么形式合适？','清单'),('我通常骑自行车通勤。','我通常怎么通勤？','自行车'),
        ('我对花生过敏。','我的饮食需要避开什么？','花生'),('我不希望你在晚上主动联系。','联系时机要注意什么？','晚上'),
        ('我习惯用摄氏度。','给我温度时用什么单位？','摄氏'),('我在学习西班牙语。','我正在学哪种外语？','西班牙'),
        ('我收藏科幻小说。','我收藏哪类书？','科幻'),('我习惯把星期一当作一周开始。','我的周历从哪天开始？','星期一')]
    for f,q,e in rows:add('facts_preferences',f,q,[e])
    projects=[('星河','需求评审','2026-10-29'),('灯塔','接口联调','2026-11-02'),('青禾','数据迁移','2026-11-05'),('云帆','上线演练','2026-11-08'),('山海','测试回归','2026-11-10')]
    for name,stage,day in projects:
        add('events_projects',f'{name}项目当前处于{stage}阶段。',f'{name}项目现在进展到哪里？',[stage])
        add('events_projects',f'{name}项目约定在{day}演示。',f'{name}项目什么时候演示？',[day])
        add('events_projects',f'{name}项目的接口负责人是陈工。',f'{name}的接口由谁负责？',['陈工'])
        add('events_projects',f'{name}项目的阻塞问题是测试环境缺少数据。',f'{name}项目被什么问题阻塞？',['数据'])
        add('events_projects',f'2026-10-20我们讨论过{name}项目的备份策略。',f'我们讨论过{name}的什么策略？',['备份'])
    updates=[('居住城市','北京','上海'),('通勤方式','开车','地铁'),('工作岗位','测试','后端开发'),('主要语言','Java','Python'),('项目阶段','需求','联调'),('常用编辑器','Vim','VS Code'),('报告格式','Word','Markdown'),('偏好饮品','咖啡','乌龙茶'),('锻炼时间','早上','晚上'),('项目负责人','张工','李工')]
    for field,old,new in updates:
        add('updates_temporal',f'2025年我的{field}是{old}。',f'我当前的{field}是什么？',[new],[],{'kind':'correct','content':f'从2026-10-01开始，我的{field}改为{new}；{old}是历史情况。'})
        add('updates_temporal',f'2025年我的{field}是{old}。',f'2025年我的{field}是什么？',[old],[],{'kind':'correct','content':f'从2026-10-01开始，我的{field}改为{new}；2025年是{old}。'})
    for ordinary,exception in [('喝茶','喝咖啡'),('骑车','打车'),('晚上阅读','早上阅读'),('吃清淡食物','吃辣菜'),('用中文','用英文')]:
        add('updates_temporal',[f'我通常{ordinary}。',f'我只在2026-10-28这一天{exception}，平常习惯不变。'],'我的平常习惯是什么？',[ordinary])
    subjects=[('朋友小王','失眠'),('同事小李','喜欢咖啡'),('妹妹','在学日语'),('父亲','喜欢钓鱼'),('客户陈先生','住在南京'),('朋友阿兰','养狗'),('同学赵明','去了成都'),('邻居','从事设计'),('同事小周','对花生过敏'),('队友阿宁','计划离职')]
    for subject,fact in subjects:
        add('subjects_quotes',f'{subject}{fact}，这是对方的情况，不是我的。',f'谁{fact}？',[subject])
        add('subjects_quotes',f'假设小说角色“{subject}”{fact}，纯属虚构，不能当成我的真实经历。',f'你有依据认为我{fact}吗？',[],['我确实'+fact],{'kind':'uncertain'})
    unknowns=['生日','学历','收入','家庭住址','手机号','身份证号','银行账号','父母姓名','婚姻状况','体重','下一次旅行日期','体检结果','昨天演示的结果','是否买过某只股票','宠物年龄','导师名字','最近一次航班','明天会议的结果','工作单位全称','好友的电话']
    for field in unknowns:add('unknown_disputed',[],f'你知道我的{field}吗？',[],[],{'kind':'uncertain'})
    for i,fact in enumerate(['我喜欢绿茶','我住在苏州','我在学法语','我叫沈知','我喜欢攀岩','我养了一只兔子','我负责蓝桥项目','我喜欢古典乐','我常用Rust','我周五去画画','我偏好长报告','我爱吃苹果','我在做数据库迁移','我常用Linux','我计划去厦门']):
        add('deletion',fact+'。','你还记得刚才要求忘记的个人信息吗？',[],[fact],{'kind':'delete'})
    for i,token in enumerate(['海盐蓝','雾松绿','晨光橙','银杏黄','晚霞紫','月白','远山灰','湖水青','玫瑰红','深海蓝','暖沙色','橄榄绿','琥珀金','丁香紫','松石青']):
        add('restart_redelivery',f'我给测试项目起的名字是{token}。','我的测试项目叫什么？',[token],[],{'kind':'restart' if i<8 else 'redelivery'})
    assert len(cases)==150
    return cases


if __name__=='__main__':
    target=Path(__file__).with_name('memory-150.jsonl')
    target.write_text('\n'.join(json.dumps(c,ensure_ascii=False) for c in build())+'\n',encoding='utf-8')
    print('Wrote',len(build()),'distinct questions to',target)
