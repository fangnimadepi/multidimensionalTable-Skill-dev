"""Public statuses for the host-model continuation protocol."""


class HostTaskPending(ValueError):
    def __init__(self, model_task):
        super().__init__("等待当前会话模型完成指定阶段。")
        self.task = model_task


class HostReportFailure(ValueError):
    def __init__(self, directory, stage, feedback):
        super().__init__("本项目的内容检查未通过，已保留材料和完成步骤。")
        self.directory, self.stage, self.feedback = directory, stage, feedback

    def details(self):
        return {"error_code": "host_content_check_failed", "retryable": False,
                "diagnostics": str(self.directory), "validation_stage": "host/" + self.stage,
                "validation_file": str(self.feedback),
                "next_action": "查看反馈后告知用户；不要自动更换 API、删除缓存或反复重跑。"}
