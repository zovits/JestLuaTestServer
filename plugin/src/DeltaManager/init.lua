--!strict
local HttpService = game:GetService("HttpService")
local DataModelDeltaService = game:GetService("DataModelDeltaService")

local logger = require(script:FindFirstChild("Logger")).new()

type RequestId = string
type Ok<T> = { success: true, result: T? }
type Err<E> = { success: false, error: E }
type Outcome<T, E> = Ok<T> | Err<E>
type ServerConfig = {
	host: string,
	port: number,
	log_level: string,
	bearer_token: string?,
}

local DeltaManager = {}
DeltaManager.__index = DeltaManager

type DeltaManagerData = {
	serverConfig: ServerConfig,
	serverUrl: string,

	sseClient: WebStreamClient?,
	sseClientConnections: { [string]: RBXScriptConnection },
	active: boolean,
	reconnectAttempts: number,
	maxReconnectAttempts: number,
	maxReconnectDelay: number,

	heartbeatTask: thread?,

	-- Store initial state for reset functionality
	initialPlaceId: number?,
}

export type DeltaManager = typeof(setmetatable({} :: DeltaManagerData, DeltaManager))

function DeltaManager.init(): DeltaManager
	local serverConfigModule = (script.Parent :: Instance):FindFirstChild("serverConfig")
	if not serverConfigModule then
		logger:fatal(
			"serverConfig module not found. Please ensure it is present in the same directory as DeltaManager."
		)
	end

	local serverConfig = require(serverConfigModule)
	logger:setLevel(serverConfig.log_level)

	local self = setmetatable({
		serverConfig = serverConfig,
		serverUrl = `http://{serverConfig.host}:{serverConfig.port}`,

		active = false,
		reconnectAttempts = 0,
		maxReconnectAttempts = 10,
		maxReconnectDelay = 30,
		sseClientConnections = {},
		heartbeatTask = nil,
		initialPlaceId = game.PlaceId,
	}, DeltaManager) :: DeltaManager

	self:start()

	-- Kill switch for development/debugging
	local KillSwitch = workspace:FindFirstChild("KillSwitch")
	if KillSwitch then
		KillSwitch.Changed:Connect(function()
			self:stop()
		end)
	end

	return self
end

function DeltaManager.applyDelta(self: DeltaManager, requestId: RequestId, delta: string): Outcome<nil, string>
	logger:info(`Applying delta for request {requestId}...`)

	local success, errorMessage = pcall(function()
		DataModelDeltaService:ApplyDelta(delta)
	end)

	if not success then
		logger:warn(`Delta application failed for {requestId}:`, errorMessage)
		return {
			success = false,
			error = tostring(errorMessage),
		}
	end

	logger:info(`Delta applied successfully for request {requestId}`)
	return {
		success = true,
		result = nil,
	}
end

function DeltaManager.resetExperience(self: DeltaManager, requestId: RequestId): Outcome<nil, string>
	logger:info(`Resetting experience for request {requestId}...`)

	-- Reload the place to reset to initial state
	-- This triggers the server to reload Studio with the original place file
	local success, errorMessage = pcall(function()
		-- Clear any dynamic content in workspace (except camera and terrain)
		for _, child in workspace:GetChildren() do
			if child:IsA("Camera") or child:IsA("Terrain") then
				continue
			end
			-- Skip the kill switch if it exists
			if child.Name == "KillSwitch" then
				continue
			end
			child:Destroy()
		end

		-- Clear other common containers that might have dynamic content
		local containers = {
			game:GetService("ReplicatedStorage"),
			game:GetService("ServerStorage"),
			game:GetService("ServerScriptService"),
		}

		for _, container in containers do
			for _, child in container:GetChildren() do
				-- Don't destroy DevPackages or Packages (dependencies)
				if child.Name == "DevPackages" or child.Name == "Packages" then
					continue
				end
				child:Destroy()
			end
		end
	end)

	if not success then
		logger:warn(`Reset failed for {requestId}:`, errorMessage)
		return {
			success = false,
			error = tostring(errorMessage),
		}
	end

	logger:info(`Experience reset successfully for request {requestId}`)
	return {
		success = true,
		result = nil,
	}
end

function DeltaManager.reportOutcome(
	self: DeltaManager,
	requestId: RequestId,
	eventType: string,
	outcome: Outcome<nil, string>
): boolean
	if outcome.success then
		logger:info(`{eventType} {requestId} completed successfully`)
	else
		logger:warn(`{eventType} {requestId} failed:`, outcome.error)
	end

	local success, response = pcall(HttpService.RequestAsync, HttpService, {
		Url = `{self.serverUrl}/_delta_result`,
		Method = "POST" :: "POST",
		Headers = {
			["Content-Type"] = "application/json",
			["Authorization"] = `Bearer {self.serverConfig.bearer_token}`,
		},
		Body = HttpService:JSONEncode({
			request_id = requestId,
			success = outcome.success,
			error = if outcome.success then nil else outcome.error,
		}),
		Compress = Enum.HttpCompression.None,
	})

	if not success then
		logger:warn("Failed to report outcome:", response)
		return false
	end

	logger:info("Reported outcome for", requestId)
	return true
end

function DeltaManager.sendHeartbeat(self: DeltaManager): boolean
	local success, response = pcall(HttpService.RequestAsync, HttpService, {
		Url = `{self.serverUrl}/_heartbeat`,
		Method = "POST" :: "POST",
		Headers = {
			["Content-Type"] = "application/json",
			["Authorization"] = `Bearer {self.serverConfig.bearer_token}`,
		},
		Body = "{}",
		Compress = Enum.HttpCompression.None,
	})

	if not success then
		logger:debug("Failed to send heartbeat:", response)
		return false
	end

	logger:trace("Sent heartbeat to server")
	return true
end

function DeltaManager.startHeartbeat(self: DeltaManager)
	if self.heartbeatTask then
		return
	end

	self.heartbeatTask = task.spawn(function()
		while self.active do
			self:sendHeartbeat()
			task.wait(1)
		end
	end)
	logger:debug("Started heartbeat task")
end

function DeltaManager.stopHeartbeat(self: DeltaManager)
	if self.heartbeatTask then
		task.cancel(self.heartbeatTask)
		self.heartbeatTask = nil
		logger:debug("Stopped heartbeat task")
	end
end

function DeltaManager.awaitHealthyServer(self: DeltaManager)
	local maxDelay = 30
	local attempts = 0
	local baseDelay = 0.5

	while true do
		local healthSuccess, healthResponse = pcall(HttpService.RequestAsync, HttpService, {
			Url = `{self.serverUrl}/health`,
			Method = "GET" :: "GET",
			Compress = Enum.HttpCompression.None,
		})
		attempts = attempts + 1

		if healthSuccess and healthResponse.StatusCode == 200 then
			logger:info(`Found healthy server at {self.serverUrl} in {attempts} attempts`)
			break
		end

		local delay = math.min(baseDelay * (2 ^ (attempts - 1)), maxDelay)

		if attempts == 1 then
			logger:warning("Waiting for server to become healthy...")
		elseif attempts % 5 == 0 then
			logger:warning(`Still waiting for server (attempt {attempts}, next check in {delay} seconds)...`)
		end

		task.wait(delay)
	end
end

function DeltaManager.reconnectWithBackoff(self: DeltaManager)
	self.reconnectAttempts += 1

	if self.reconnectAttempts > self.maxReconnectAttempts then
		logger:error(`Maximum reconnection attempts ({self.maxReconnectAttempts}) reached. Stopping reconnection.`)
		self:stop()
		return
	end

	local reconnectDelay = if self.reconnectAttempts == 1
		then 0
		else math.min(2 ^ (self.reconnectAttempts - 1), self.maxReconnectDelay)

	if reconnectDelay > 0 then
		logger:warning(
			`Reconnecting SSE client in {reconnectDelay} seconds (attempt {self.reconnectAttempts}/{self.maxReconnectAttempts})...`
		)
		task.wait(reconnectDelay)
	else
		logger:warning(
			`Reconnecting SSE client immediately (attempt {self.reconnectAttempts}/{self.maxReconnectAttempts})...`
		)
	end

	if self.active then
		local sseClient = self:connectSSEClient()
		if sseClient then
			logger:info("SSE connection restored after", self.reconnectAttempts, "attempts")
			self.reconnectAttempts = 0
		else
			self:reconnectWithBackoff()
		end
	else
		logger:warning("Cancelling scheduled reconnect since client is no longer active")
	end
end

function DeltaManager.connectSSEClient(self: DeltaManager): WebStreamClient?
	local success, sseClient = pcall(function()
		return HttpService:CreateWebStreamClient(Enum.WebStreamClientType.SSE, {
			Url = `{self.serverUrl}/_events`,
			Headers = {
				["Content-Type"] = "text/event-stream",
				["Authorization"] = `Bearer {self.serverConfig.bearer_token}`,
			},
			Method = "GET",
		})
	end)

	if not success then
		logger:error("Failed to create SSE client: " .. tostring(sseClient))
		return nil
	else
		logger:info("Connected SSE Client to server")
	end

	-- Cleanup old connections
	for _, connection in self.sseClientConnections do
		connection:Disconnect()
	end

	self.sseClientConnections.MessageReceived = sseClient.MessageReceived:Connect(function(message)
		self:handleSSEMessage(message)
	end)
	self.sseClientConnections.Error = sseClient.Error:Connect(function(code, message)
		logger:warning("SSE error", code, message)
	end)
	self.sseClientConnections.Closed = sseClient.Closed:Connect(function()
		if self.active then
			logger:warning("SSE connection closed unexpectedly")
			self:reconnectWithBackoff()
		else
			logger:info("SSE connection closed")
		end
	end)

	self.sseClient = sseClient

	return sseClient
end

function DeltaManager.handleSSEMessage(self: DeltaManager, message: string)
	logger:trace("Received SSE message:", message)

	local event = string.match(message, "event:%s*(.-)%s*\n")
	if event == "ping" or event == nil then
		logger:trace("Treating message as a keep-alive ping")
		return
	end

	local raw_data = string.match(message, "data:%s*(.-)%s*\n")
	local data = if raw_data then HttpService:JSONDecode(raw_data) else {}
	logger:trace(data)

	if event == "delta_apply" then
		logger:debug(`Received delta_apply for request {data.request_id}`)
		task.spawn(function()
			local outcome = self:applyDelta(data.request_id, data.delta)
			self:reportOutcome(data.request_id, "delta_apply", outcome)
		end)
	elseif event == "reset" then
		logger:debug(`Received reset for request {data.request_id}`)
		task.spawn(function()
			local outcome = self:resetExperience(data.request_id)
			self:reportOutcome(data.request_id, "reset", outcome)
		end)
	elseif event == "shutdown" then
		logger:info("Server is shutting down")
		self:stop()
	else
		logger:warning("unhandled event type:", event)
	end
end

function DeltaManager.start(self: DeltaManager)
	self.active = true
	self:awaitHealthyServer()
	self:connectSSEClient()
	self:startHeartbeat()
end

function DeltaManager.stop(self: DeltaManager)
	self.active = false
	self:stopHeartbeat()
	if self.sseClient then
		self.sseClient:Close()
	end
	for _, connection in self.sseClientConnections do
		connection:Disconnect()
	end
	logger:info("DeltaManager stopped")
end

return DeltaManager

