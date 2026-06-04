-- SceneCameraInput -- generic first-person camera/input service (PR5).
--
-- The deterministic home for the first-person camera/input fidelity cluster
-- in GENERIC scene-runtime mode (replacing the legacy FPS coherence packs:
-- camera yaw inheritance, pitch composition, default-controls-off/body-hide,
-- the E2E mouse channel). Reached by transpiled controllers via the
-- camera-facet lowering pass (see docs/design/camera-input-fidelity-plan.md),
-- NOT by editing the AI's emitted look-math in place.
--
-- WHY a service: Roblox's camera is ``workspace.CurrentCamera`` -- a singleton
-- not in the workspace tree, so Unity's camera-child-of-player yaw inheritance
-- can't be expressed as nesting and must be re-composed each frame. Doing that
-- in one vetted place (here) is deterministic; leaving it to the AI per-game
-- is the bug this replaces.
--
-- SCOPE CAP (load-bearing -- do NOT cross): camera pose composition
-- (world-yaw o local-pitch), pitch clamp, recoil, default-controls-off +
-- body-hide, the E2E mouse channel, and a read-only look for game logic. It
-- yaws the player RIG (so the body faces the view -> movement basis stays in
-- the rig pivot, which the controller's Move already reads). It does NOT own
-- WASD translation, weapons, or CharacterController emulation. "Owning
-- locomotion = rebuilding Unity."
--
-- ORDERING: ``step(dt)`` is called IN-BAND from the controller's Update (where
-- the look method used to run), so the per-frame order is preserved:
-- Shoot (recoil -> applyRecoil) -> step (yaw applied to rig) -> Move (reads the
-- rig pivot). The service runs NO separate RenderStepped/Heartbeat loop -- that
-- would race the controller's Update and reintroduce one-frame yaw/move skew.

local RunService = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")
local Players = game:GetService("Players")

local SceneCameraInput = {}
SceneCameraInput.__index = SceneCameraInput

-- ---------------------------------------------------------------------------
-- Pure helpers (no Roblox singletons beyond CFrame/Vector3 math) -- unit-tested
-- under the luau host harness. Keep all engine I/O out of these.
-- ---------------------------------------------------------------------------

-- World position + body world-yaw + local pitch, mirroring Unity's
-- parent(yaw) -> child(pitch) camera hierarchy.
local function composeLook(eyePos, yawRad, pitchRad)
	return CFrame.new(eyePos)
		* CFrame.Angles(0, yawRad, 0)
		* CFrame.Angles(pitchRad, 0, 0)
end
SceneCameraInput._composeLook = composeLook

-- Advance accumulated yaw/pitch from a raw mouse delta. Yaw is unbounded
-- (wraps naturally through CFrame); pitch is clamped. Roblox
-- GetMouseDelta().Y is positive-DOWN, so subtracting dy gives the canonical
-- "push forward -> look up" feel. Returns (yaw, pitch).
local function advance(yawRad, pitchRad, dx, dy, sensitivity, minPitch, maxPitch)
	yawRad = yawRad - dx * sensitivity
	pitchRad = math.clamp(pitchRad - dy * sensitivity, minPitch, maxPitch)
	return yawRad, pitchRad
end
SceneCameraInput._advance = advance

-- ---------------------------------------------------------------------------
-- Singleton (one CurrentCamera per client)
-- ---------------------------------------------------------------------------

local _instance = nil

function SceneCameraInput.acquire()
	if not _instance then
		_instance = setmetatable({
			_yaw = 0,
			_pitch = 0,
			_rig = nil,
			-- radians per pixel; tuned in e2e. ~0.0045 ≈ a 60°-FOV-ish feel.
			_sensitivity = 0.0045,
			_minPitch = math.rad(-80),
			_maxPitch = math.rad(80),
			_eyeHeight = 1.5,
			_initialized = false,
		}, SceneCameraInput)
	end
	return _instance
end

-- Extract the yaw (Y) component of a world CFrame so the service seeds from the
-- rig's authored spawn orientation instead of snapping to 0 on first frame.
local function yawOf(cframe)
	local _, y = cframe:ToEulerAnglesYXZ()
	return y
end

function SceneCameraInput:configure(opts)
	opts = opts or {}
	if opts.rig ~= nil then
		self._rig = opts.rig
		-- Seed yaw from the rig's authored orientation (once).
		if not self._seeded and opts.rig then
			local pivot = self:_rigPivot()
			if pivot then
				self._yaw = yawOf(pivot)
				self._seeded = true
			end
		end
	end
	if opts.sensitivity ~= nil then self._sensitivity = opts.sensitivity end
	if opts.minPitch ~= nil then self._minPitch = opts.minPitch end
	if opts.maxPitch ~= nil then self._maxPitch = opts.maxPitch end
	if opts.eyeHeight ~= nil then self._eyeHeight = opts.eyeHeight end
	self:_ensureInit()
	return self
end

function SceneCameraInput:_rigPivot()
	local rig = self._rig
	if not rig then return nil end
	if rig.GetPivot then return rig:GetPivot() end
	if rig.CFrame then return rig.CFrame end
	return nil
end

-- One-time client setup: take authoritative camera control + FPS mouse state +
-- hide the local default character (folds the fps_default_controls_off job).
function SceneCameraInput:_ensureInit()
	if self._initialized then return end
	if not RunService:IsClient() then return end
	self._initialized = true

	local cam = workspace.CurrentCamera
	if cam then cam.CameraType = Enum.CameraType.Scriptable end
	UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
	UserInputService.MouseIconEnabled = false

	-- Disable Roblox's default PlayerModule controls (it re-asserts
	-- MouseBehavior each frame otherwise) and hide the local default avatar,
	-- which is not the scene player rig.
	local lp = Players.LocalPlayer
	local function applyControls()
		local ps = lp and lp:FindFirstChild("PlayerScripts")
		local pm = ps and ps:FindFirstChild("PlayerModule")
		if pm then
			local ok, mod = pcall(require, pm)
			if ok and mod then
				local ok2, controls = pcall(function() return mod:GetControls() end)
				if ok2 and controls and controls.Disable then
					pcall(function() controls:Disable() end)
				end
			end
		end
		UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter
		UserInputService.MouseIconEnabled = false
	end
	local function hideChar(char)
		if not char then return end
		local function hide(p)
			if p:IsA("BasePart") or p:IsA("Decal") then
				p.LocalTransparencyModifier = 1
			end
		end
		char.DescendantAdded:Connect(hide)
		for _, p in char:GetDescendants() do hide(p) end
	end
	if lp then
		applyControls()
		hideChar(lp.Character)
		lp.CharacterAdded:Connect(function(char)
			task.wait()
			applyControls()
			hideChar(char)
		end)
	end
end

-- Read this frame's mouse delta + the production-safe E2E channel (see
-- docs/E2E_INPUT_CHANNEL.md): additive, no-op when unset.
function SceneCameraInput:_readDelta()
	local d = UserInputService:GetMouseDelta()
	local dx, dy = d.X, d.Y
	local seq = workspace:GetAttribute("E2EMouseSeq") or 0
	if seq > (workspace:GetAttribute("E2EMouseAckSeq") or 0) then
		workspace:SetAttribute("E2EMouseAckSeq", seq)
		dx = dx + (workspace:GetAttribute("E2EMouseDeltaX") or 0)
		dy = dy + (workspace:GetAttribute("E2EMouseDeltaY") or 0)
	end
	return dx, dy
end

-- In-band per-frame update. Called where the controller's look method ran.
function SceneCameraInput:step(dt)
	self:_ensureInit()
	local dx, dy = self:_readDelta()
	self._yaw, self._pitch = advance(
		self._yaw, self._pitch, dx, dy,
		self._sensitivity, self._minPitch, self._maxPitch
	)

	-- Yaw the rig so the body faces the view; movement basis lives in the rig
	-- pivot (the controller's Move reads it unchanged). Eye = rig + eyeHeight.
	local eyePos
	local pivot = self:_rigPivot()
	if pivot then
		local rig = self._rig
		if rig and rig.PivotTo then
			rig:PivotTo(CFrame.new(pivot.Position) * CFrame.Angles(0, self._yaw, 0))
		end
		eyePos = pivot.Position + Vector3.new(0, self._eyeHeight, 0)
	else
		local cam = workspace.CurrentCamera
		eyePos = (cam and cam.CFrame.Position) or Vector3.zero
	end

	local cam = workspace.CurrentCamera
	if cam then
		cam.CFrame = composeLook(eyePos, self._yaw, self._pitch)
	end
end

-- Recoil etc. write camera pitch. ``deltaPitchRad`` is radians (the lowering
-- pass converts a degrees literal, e.g. ``camRotationX - 2`` -> rad(-2)).
function SceneCameraInput:applyRecoil(deltaPitchRad)
	self._pitch = math.clamp(self._pitch + deltaPitchRad, self._minPitch, self._maxPitch)
end

-- Movement basis for callers that don't read the rig pivot directly.
function SceneCameraInput:getYawBasis()
	return CFrame.Angles(0, self._yaw, 0)
end

-- Read-only look for game logic (raycasts). Mirrors CurrentCamera.CFrame.
function SceneCameraInput:getLookCFrame()
	local cam = workspace.CurrentCamera
	return cam and cam.CFrame or CFrame.new()
end

-- The rig was teleported (respawn / TakeDamage); resync yaw from its new pose
-- so the camera doesn't snap back to the pre-teleport facing.
function SceneCameraInput:onRespawn(cframe)
	if cframe then
		self._yaw = yawOf(cframe)
	elseif self:_rigPivot() then
		self._yaw = yawOf(self:_rigPivot())
	end
	self._pitch = 0
end

return SceneCameraInput
