"""PII-free, manually labelled dialogue intent/frame cases.

These cases are deliberately phrased in colloquial and ambiguous Chinese. They are not a
substitute for anonymized production replay; they provide a stable semantic truth set where
the expected direction is a product decision rather than the output of an older model.
"""

CURATED_CASES: list[dict] = [
    # worker: finding a job versus explicitly publishing a resume
    {"case_id": "w-job-01", "role": "worker", "text": "苏州找电子厂工作，五千以上", "expected_intent": "search_job"},
    {"case_id": "w-job-02", "role": "worker", "text": "宁波有技工的活吗", "expected_intent": "search_job"},
    {"case_id": "w-job-03", "role": "worker", "text": "想去杭州做服务员", "expected_intent": "search_job"},
    {"case_id": "w-job-04", "role": "worker", "text": "给我看看北京的保安岗位", "expected_intent": "search_job"},
    {"case_id": "w-job-05", "role": "worker", "text": "东莞食品厂六千以上包住的工作", "expected_intent": "search_job"},
    {"case_id": "w-job-06", "role": "worker", "text": "夫妻俩想找能一起进的厂", "expected_intent": "search_job"},
    {"case_id": "w-job-07", "role": "worker", "text": "有没有夜班少一点的普工活", "expected_intent": "search_job"},
    {"case_id": "w-job-08", "role": "worker", "text": "我想换个城市找活，去无锡", "expected_intent": "search_job"},
    {"case_id": "w-resume-01", "role": "worker", "text": "我要提交简历：男，32岁，想做焊工", "expected_intent": "upload_resume"},
    {"case_id": "w-resume-02", "role": "worker", "text": "帮我登记个人资料，女，28岁，大专", "expected_intent": "upload_resume"},
    {"case_id": "w-resume-03", "role": "worker", "text": "这是我的简历，我在昆山想找仓库工作", "expected_intent": "upload_resume"},
    {"case_id": "w-resume-04", "role": "worker", "text": "发布一下我的求职简历，期望月薪6500", "expected_intent": "upload_resume"},

    # factory: recruiting/searching candidates versus explicitly publishing a job
    {"case_id": "f-search-01", "role": "factory", "text": "找工人", "expected_intent": "search_worker"},
    {"case_id": "f-search-02", "role": "factory", "text": "我要招聘一个网管，北京上班", "expected_intent": "search_worker"},
    {"case_id": "f-search-03", "role": "factory", "text": "帮我找五个苏州普工", "expected_intent": "search_worker"},
    {"case_id": "f-search-04", "role": "factory", "text": "有没有会氩弧焊的师傅", "expected_intent": "search_worker"},
    {"case_id": "f-search-05", "role": "factory", "text": "想招年龄四十以内的仓管", "expected_intent": "search_worker"},
    {"case_id": "f-search-06", "role": "factory", "text": "找两个能上夜班的操作工", "expected_intent": "search_worker"},
    {"case_id": "f-job-01", "role": "factory", "text": "帮我发布岗位：苏州普工，月薪5500，招10人", "expected_intent": "upload_job"},
    {"case_id": "f-job-02", "role": "factory", "text": "登记一个招聘岗位，杭州保安，包住", "expected_intent": "upload_job"},
    {"case_id": "f-job-03", "role": "factory", "text": "岗位信息如下：北京服务员，底薪5000", "expected_intent": "upload_job"},
    {"case_id": "f-job-04", "role": "factory", "text": "把这条招工信息发出去：昆山焊工，计件", "expected_intent": "upload_job"},

    # broker: explicit object determines direction, not previous default
    {"case_id": "b-worker-01", "role": "broker", "text": "找一个电子厂流水线普工，地点杭州", "expected_intent": "search_worker"},
    {"case_id": "b-worker-02", "role": "broker", "text": "帮我找三名焊工", "expected_intent": "search_worker"},
    {"case_id": "b-worker-03", "role": "broker", "text": "想找一个电子厂工人", "expected_intent": "search_worker"},
    {"case_id": "b-worker-04", "role": "broker", "text": "招一个会开叉车的师傅", "expected_intent": "search_worker"},
    {"case_id": "b-job-01", "role": "broker", "text": "帮一个工人找苏州电子厂的活", "expected_intent": "search_job"},
    {"case_id": "b-job-02", "role": "broker", "text": "给这位师傅找个杭州焊工岗位", "expected_intent": "search_job"},
    {"case_id": "b-job-03", "role": "broker", "text": "找一个岗位：无锡普工，5500以上", "expected_intent": "search_job"},
    {"case_id": "b-job-04", "role": "broker", "text": "有个工人想在上海找工作", "expected_intent": "search_job"},
    {"case_id": "b-upload-01", "role": "broker", "text": "发布岗位：常州操作工，招20人，月薪6000", "expected_intent": "upload_job"},
    {"case_id": "b-upload-02", "role": "broker", "text": "提交一份工人简历：男，35岁，电工", "expected_intent": "upload_resume"},

    # deterministic/safe boundaries
    {"case_id": "cmd-01", "role": "worker", "text": "/帮助", "expected_intent": "command"},
    {"case_id": "cmd-02", "role": "factory", "text": "/取消", "expected_intent": "command"},
    {"case_id": "chat-01", "role": "worker", "text": "你好呀", "expected_intent": "chitchat"},
    {"case_id": "chat-02", "role": "broker", "text": "今天天气不错", "expected_intent": "chitchat"},
]
