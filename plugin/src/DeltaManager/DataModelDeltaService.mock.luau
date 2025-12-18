local ChangeHistoryService = game:GetService("ChangeHistoryService")

local DataModelDeltaService = {}

function DataModelDeltaService:ApplyDelta(delta: string)
	local recording = ChangeHistoryService:TryBeginRecording("Apply Delta")
	if not recording then
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
