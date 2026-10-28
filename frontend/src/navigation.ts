export interface NavPage { id: string; label: string; description: string; keywords: string }
export interface NavGroup { id: string; label: string; english?: string; modules: Omit<NavModule, 'group'>[] }
export interface NavModule { id: string; label: string; icon: string; description: string; pages: NavPage[]; group: { id: string; label: string; english?: string } }
  const page = (id: string, label: string, description: string, keywords = '') => ({ id, label, description, keywords });
  export const groups: NavGroup[] = [
    { id: 'daily', label: '日常使用', english: 'DAILY SPACE', modules: [
      { id: 'chat', label: '智能对话', icon: 'chat', description: '让每一次对话，延续彼此的默契。', pages: [page('conversation', '对话窗口', '这里将承载你的对话与灵感。', '聊天 文字 图片 文档 多模态 语音 chat')] },
      { id: 'memory', label: '长期记忆', icon: 'memory', description: '记住重要的事，也尊重每一次改变。', pages: [page('list', '记忆列表', '重要的人、事与偏好，将在这里被妥善记住。'), page('candidates', '候选记忆', '值得留下的片段，将在这里整理与核对。'), page('exports', '导出记录', '你的记忆，也可以以自己的方式保存。'), page('deletions', '删除进度', '查看每一次遗忘请求的处理进度。')] },
      { id: 'tasks', label: '任务中心', icon: 'tasks', description: '把想法交给行动，让事情有始有终。', pages: [page('list', '任务列表', '任务步骤、助手协作与执行结果，将在这里汇聚。', '项目 文件 子助手 并行 协作'), page('reminders', '待办提醒', '为每件约定的小事，留一个合适的时间。')] },
      { id: 'proactive', label: '主动交流', icon: 'sparkles', description: '在恰好的时刻，接续值得聊的话题。', pages: [page('topics', '待续话题', '那些尚未聊完的事，留待合适的时机。'), page('preferences', '联系偏好', '联系渠道、安静时段与交流频率，由你决定。'), page('history', '联系记录', '每一次主动联系，都有可以追溯的缘由。')] }
    ] },
    { id: 'capability', label: '能力配置', english: 'CAPABILITIES', modules: [
      { id: 'channels', label: '平台接入', icon: 'channels', description: '在熟悉的平台，遇见同一个灵犀。', pages: [page('instances', '平台实例', 'Web 与 QQ 的连接状态，将在这里统一呈现。', 'NapCat OneBot QQ'), page('rules', '渠道规则', '为 QQ 私聊与群聊设定清晰的响应规则。'), page('identities', '身份绑定', '在你确认之后，连接不同平台上的身份。')] },
      { id: 'models', label: '模型管理', icon: 'models', description: '为不同的问题，选择合适的思考方式。', pages: [page('providers', '服务连接', '模型提供商与连接配置，将在这里管理。', 'OpenAI Anthropic Gemini API 提供商'), page('catalog', '模型目录', '了解每个模型的能力、用途与可用范围。'), page('policies', '路由策略', '配置用途选模、能力筛选与授权范围内的降级。'), page('calls', '调用记录', '模型版本、用量与调用结果，将在这里留痕。')] },
      { id: 'assistants', label: '助手管理', icon: 'assistants', description: '表达有自己的温度，协作有清晰的分工。', pages: [page('personas', '人格管理', '配置灵犀的表达风格、语言与音色偏好。'), page('roles', '执行角色', '为研究、规划与执行赋予合适的专长。'), page('agents', '助手配置', '配置子助手、角色组合与协作策略。', '子助手 Agent 编排'), page('support', '支持偏好', '倾听、建议或直接行动，选择你需要的支持。', '情绪 关怀 心情 表达')] },
      { id: 'plugins', label: '插件中心', icon: 'plugins', description: '让能力不断生长，让每一次授权清清楚楚。', pages: [page('catalog', '插件商店', '发现适合自己的能力扩展。'), page('installed', '已装插件', '已安装插件的配置、状态与依赖，将在这里汇聚。'), page('skills', '技能工具', '查看工具与 Skills，以及提供这些能力的插件。', 'Skill MCP API'), page('connections', '账号授权', '集中查看插件账号连接与已授权范围。'), page('updates', '更新回滚', '了解版本与权限变化，再决定下一步。'), page('invocations', '运行记录', '查看插件调用结果、回执与相关任务。')] }
    ] },
    { id: 'system', label: '系统管理', english: 'SYSTEM SPACE', modules: [
      { id: 'data', label: '数据日志', icon: 'data', description: '让数据有归属，让每一步都可以追溯。', pages: [page('files', '文件管理', '上传附件、受控文件与任务产物，将在这里整理。'), page('traces', '执行追踪', '沿着消息、模型、助手和工具，回看执行过程。'), page('logs', '系统日志', '查看经过脱敏处理的系统运行记录。'), page('usage', '用量预算', '了解模型、插件与任务的用量和费用。'), page('audit', '操作审计', '关键配置与授权变更，将在这里留痕。'), page('diagnostics', '诊断导出', '按授权范围，准备经过脱敏的诊断资料。')] },
      { id: 'settings', label: '系统设置', icon: 'settings', description: '照顾好工作空间，让陪伴稳稳持续。', pages: [page('overview', '系统概览', '服务器与各项能力的健康情况，将在这里呈现。'), page('general', '系统配置', '管理分层配置、版本与生效范围。'), page('voice', '语音配置', '设置默认语言、音色、语速与语音模式。', 'ASR TTS 麦克风'), page('media', '媒体配置', '管理附件类型、大小限制与解析配置。'), page('security', '安全设置', '管理认证、登录会话与安全选项。'), page('resources', '资源授权', '明确服务器资源与可操作目录的范围。'), page('backups', '备份恢复', '妥善保存工作空间，并核验恢复结果。'), page('migration', '系统迁移', '在服务器之间，有序迁移工作空间。')] }
    ] }
  ];
  const storeGroups: Omit<NavModule, 'group'>[] = [
    { id: 'store-developer', label: '开发后台', icon: 'code', description: '为灵犀带来新的可能。', pages: [page('plugins', '我的插件', '插件资料与版本，将在独立商店后台管理。'), page('submissions', '提交记录', '查看版本提交、审核进度与反馈。')] },
    { id: 'store-review', label: '审核治理', icon: 'shield', description: '让每一份能力，都经得起信任。', pages: [page('queue', '审核队列', '查看待审核版本与自动检查结果。'), page('releases', '版本治理', '管理发布、下架和版本撤销。'), page('trust', '签名信任', '管理签名信任、密钥轮换与撤销信息。'), page('audit', '审核审计', '追溯商店审核与版本治理操作。')] }
  ];
  export const modules: NavModule[] = groups.flatMap(group => group.modules.map(module => ({ ...module, group })));
  export const store: NavModule[] = storeGroups.map(module => ({ ...module, group: { id: 'store', label: '商店后台', english: 'PLUGIN STORE' } }));
  export const routes = [...modules, ...store].flatMap(module => module.pages.map((item, index) => ({ ...item, module, index, href: `#/${module.id}/${item.id}` })));
