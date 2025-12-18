local ChangeHistoryService = game:GetService("ChangeHistoryService")

local DataModelDeltaService = {}

function DataModelDeltaService:ApplyDelta(delta: string)
	local recording = ChangeHistoryService:TryBeginRecording("Apply Delta")
	if not recording then
		-- Handle error here. This indicates that your plugin began a previous
		-- recording and never completed it. You may only have one recording
		-- per plugin active at a time.
		return
	end

	local materialEnums = Enum.Material:GetEnumItems()
	local material = materialEnums[math.random(1, #materialEnums)]

	for _, part in workspace:GetDescendants() do
		if not part:IsA("BasePart") then
			continue
		end
		part.Material = material
	end

	ChangeHistoryService:FinishRecording(recording, Enum.FinishRecordingOperation.Commit)

	task.wait(1) -- Let studio render
end

function DataModelDeltaService:UndoDelta(delta: string)
	ChangeHistoryService:Undo()
end

return DataModelDeltaService
