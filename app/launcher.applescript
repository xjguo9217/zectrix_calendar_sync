-- Zectrix 同步.app 的主体
--
-- 双击后：跑同一个 run.sh，把输出写进日志，最后弹一条通知说同步了什么。
-- 关键点是它用 (path to me) 找项目目录，所以整个文件夹拷到别的电脑一样能用，
-- 不写死任何绝对路径。

on run
	set appPosix to POSIX path of (path to me)
	set projectDir to do shell script "dirname " & quoted form of appPosix
	set logFile to (POSIX path of (path to home folder)) & "Library/Logs/zectrix-sync.log"

	if (do shell script "test -x " & quoted form of (projectDir & "/run.sh") & " && echo yes || echo no") is "no" then
		display dialog "找不到 run.sh。" & return & return & ¬
			"「Zectrix 同步.app」必须和 sync_calendar.py 放在同一个文件夹里，" & ¬
			"别把 app 单独拖出去。桌面上放替身（alias）就行。" & return & return & ¬
			"当前位置：" & projectDir buttons {"好"} default button 1 with icon caution
		return
	end if

	display notification "正在同步…" with title "Zectrix 同步"

	-- 末尾的 echo 保证整条命令永远退出 0，这样 do shell script 不会抛异常，
	-- 我们自己解析真实退出码。
	set shellCmd to "cd " & quoted form of projectDir & " && " & ¬
		"{ date '+=== %Y-%m-%d %H:%M:%S ==='; ./run.sh; echo \"EXITCODE:$?\"; } >> " & ¬
		quoted form of logFile & " 2>&1; tail -n 400 " & quoted form of logFile

	set logTail to do shell script shellCmd

	set exitCode to my lastValueAfter(logTail, "EXITCODE:")
	set summaryText to my lastValueAfter(logTail, "SUMMARY: ")
	if summaryText is "" then set summaryText to "没读到同步结果，看看日志"

	if exitCode is "0" then
		display notification summaryText with title "Zectrix 同步完成" sound name "Glass"
		return
	end if

	-- 出错了：权限问题最常见，单独给一个直达设置的按钮
	if logTail contains "没有提醒事项访问权限" then
		display dialog "同步没能读写「提醒事项」。" & return & return & ¬
			"请在「隐私与安全性 → 提醒事项」里，为「Zectrix 同步」打开开关，然后再点一次。" ¬
			buttons {"稍后", "打开隐私设置"} default button 2 with icon caution
		if button returned of result is "打开隐私设置" then
			do shell script "open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Reminders'"
		end if
		return
	end if

	display dialog "同步没跑完。" & return & return & summaryText ¬
		buttons {"好", "查看日志"} default button 2 with icon caution
	if button returned of result is "查看日志" then
		do shell script "open -a Console " & quoted form of logFile
	end if
end run


-- 从日志里取某个前缀最后一次出现时后面的内容
on lastValueAfter(theText, thePrefix)
	set foundValue to ""
	repeat with aLine in paragraphs of theText
		set lineText to aLine as text
		if lineText starts with thePrefix then
			set foundValue to text ((length of thePrefix) + 1) thru -1 of lineText
		end if
	end repeat
	return foundValue
end lastValueAfter
