local DataModelDeltaService = {}

function DataModelDeltaService:ApplyDelta(delta: string)
	print("Applying delta:", delta)
end

function DataModelDeltaService:UndoDelta(delta: string)
	print("Undoing delta:", delta)
end

return DataModelDeltaService
