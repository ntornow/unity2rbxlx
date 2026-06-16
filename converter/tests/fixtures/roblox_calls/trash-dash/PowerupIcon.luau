local PowerupIcon = {}
PowerupIcon.__index = PowerupIcon

function PowerupIcon.new(config)
	local self = setmetatable({}, PowerupIcon)
	-- Pure constructor: scalar config only. linkedConsumable, icon (Image) and
	-- slider (Slider) are scene references injected on self by the host before Awake.
	-- linkedConsumable is set at runtime by the spawning script ([HideInInspector]).
	return self
end

function PowerupIcon:Start()
	-- icon.sprite = linkedConsumable.icon
	-- linkedConsumable.icon is the consumable's asset id (rbxassetid string).
	if self.icon ~= nil and self.linkedConsumable ~= nil then
		self.icon.Image = self.linkedConsumable.icon
	end
end

function PowerupIcon:Update(dt)
	-- slider.value = 1.0f - linkedConsumable.timeActive / linkedConsumable.duration
	-- timeActive is a getter method on the converted Consumable; duration is a field.
	if self.slider ~= nil and self.linkedConsumable ~= nil then
		local duration = self.linkedConsumable.duration
		local value = 1.0 - self.linkedConsumable:timeActive() / duration
		self.slider.value = value
	end
end

return PowerupIcon