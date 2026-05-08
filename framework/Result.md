整体流程分为预处理preprocessing和runtime两部分

preprocessing: 最终会输出到/output中，以wpkg的形式，最后解析成json供runtime端使用，目前解析的粒度是合理的，但是输出的内容经常出现null，初步怀疑跟小说类型相关

runtime

游玩侧（主要描述LLM交互，具体的SSE，以及业务层面的开始游戏，继续游戏，存档/读档先不考虑）：
  1.无玩家输入且不触发工具调用情况下--定义为（NoInputAndNoTool）：
  1.1 Setup 场景分析{Setup阶段的Prompt，以及必要的信息} -> LLM回复{"ready_for_writer": true, "writing_guidance": "..."} 
  1.2 Write 正文书写{Write对应的Prompt，同时携带1.1返回的writing_guidance} -> LLM回复{正文...} -> 使用SSE返回的正文响应
  1.3 L0Compress 记忆压缩（在整个事件的Setup+Confrontation+Resolution全部结束后同步执行一次）
      {l0_compress.txt Prompt + event_id + original_text(setup_narrative+confrontation_history拼接) + original_char_count + min_summary_length(原文30%)}
      -> LLM回复{"summary": "...", "tags": [...]} -> 追加到内存 l0_summaries -> 存档时序列化到本地 JSON
      -> 推进到下一事件 -> 1.1
  1.3.1 L1Compress 记忆压缩：在 L0 满 10 条后异步后台执行，不阻塞游戏流程（ThreadPoolExecutor 后台任务）

  2.无玩家输入但触发工具调用情况--定义为（NoInputAndTool）：
  2.1 Confrontation 工具决策
