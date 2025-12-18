local DeltaManager = require(script.Parent.DeltaManager)

task.wait(4) -- Wait for Studio to finish loading content and rendering
DeltaManager.init()
